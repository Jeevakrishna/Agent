"""PRCA LangGraph compliance-check agent graph.

Five-node StateGraph:

  RuleChange
      │
      ▼
  retrieve    — embed + pgvector search; load projects + latest design snapshot
      │
      ▼
  classify    — LLM (with 3× retry) labels change_type + affected attributes.
      │         Falls back to a best-effort guess (no change_type) if LLM is
      │         unavailable — deterministic checks downstream still run.
      ▼
  impact      — HYBRID CORE.
      │         a) Deterministic numeric comparisons for extractable thresholds.
      │         b) LLM fallback with 0.75 confidence cap for textual rules only.
      │         Writes to compliance_findings table.
      ▼
  alert       — Assembles final ComplianceFinding objects with full explanations.
      │
      ▼
  gate        — Conditional edge: high-confidence flags → notify queue;
                everything else → needs_review queue.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import psycopg
from langgraph.graph import END, StateGraph

# Make `python path/to/graph.py` and `python -m agent.graph` both work.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from agent.config import get_settings  # noqa: E402
from agent.db import (  # noqa: E402
    get_connection,
    get_jurisdiction_by_name,
    get_projects_with_latest_design,
    insert_compliance_finding,
    insert_rule_change,
    search_rules,
)
from agent.embeddings import embed  # noqa: E402
from agent.llm import get_chat_model  # noqa: E402
from agent.models import (  # noqa: E402
    CLASSIFY_PROMPT,
    ClassificationOutput,
    ComplianceFinding,
    DISCLAIMER_SENTENCE,
    DesignAttributes,
    FindingStatus,
    GraphState,
    IMPACT_LLM_PROMPT,
    ProjectWithContext,
    RuleChange,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Human-review trust boundary.
#
# Deterministic numeric checks produce confidence 0.90+. Any LLM fallback
# caps at 0.75, so this 0.80 cleanly separates the two paths: results we're
# willing to auto-raise as a compliance flag vs. results that must be
# reviewed by a human before surfacing.
#
# Tuning this number is a policy decision, not a technical one — if you
# want to be more conservative, raise it toward 0.95; if you want more
# aggressive auto-flagging (and more false positives), lower it toward 0.7.
AUTO_FLAG_CONFIDENCE_THRESHOLD = 0.80

DETERMINISTIC_ATTRIBUTES: set[str] = {
    "fire_resistance_rating_hours",
    "setback_ft",
    "max_occupant_load",
    "stories",
}

# Numeric-threshold regexes — try to pull "X hours", "X ft", "X feet", "X stories",
# "X persons", "X occupants" out of the rule text. Prefer the LARGEST number
# in each match because regulations state a MINIMUM / MAXIMUM threshold.
_NUM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "fire_resistance_rating_hours",
        re.compile(
            r"(?P<num>\d+(?:\.\d+)?)\s*-?\s*(?:hour|hr|hours|hrs|h)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "setback_ft",
        re.compile(
            r"(?P<num>\d+(?:\.\d+)?)\s*-?\s*(?:foot|feet|ft|')\b",
            re.IGNORECASE,
        ),
    ),
    (
        "max_occupant_load",
        re.compile(
            r"(?P<num>\d+)\s*-?\s*(?:person|persons|occupant|occupants|people)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "stories",
        re.compile(
            r"(?P<num>\d+)\s*-?\s*(?:story|stories|storey|storeys|floors?)\b",
            re.IGNORECASE,
        ),
    ),
]


# ---------------------------------------------------------------------------
# 1. retrieve_node
# ---------------------------------------------------------------------------
def retrieve_node(state: GraphState) -> GraphState:
    """Embed the incoming rule text, search rules, load projects + designs."""
    errors: list[str] = []
    rule_change: RuleChange = state["rule_change"]

    # Look up jurisdiction_id from name (optional but speeds up retrieval).
    jurisdiction_id: Optional[str] = None
    try:
        jrow = get_jurisdiction_by_name(rule_change.jurisdiction)
        if jrow:
            jurisdiction_id = jrow["id"]
    except psycopg.Error as exc:
        errors.append(f"retrieve: jurisdiction lookup failed: {exc}")

    # Embed new_text -> pgvector cosine search (top 10).
    retrieved_rules: list[dict[str, Any]] = []
    try:
        vectors = embed([rule_change.new_text])
        if vectors:
            retrieved_rules = search_rules(
                vectors[0],
                jurisdiction_id=jurisdiction_id,
                limit=10,
            )
    except Exception as exc:  # embeddings or DB
        errors.append(f"retrieve: rule search failed: {exc!r}")

    # Load projects + latest design snapshot (jurisdiction filter if possible).
    matched_projects: list[ProjectWithContext] = []
    try:
        rows = get_projects_with_latest_design(jurisdiction_id=jurisdiction_id)
        for r in rows:
            payload: dict[str, Any] = r.get("design_payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            matched_projects.append(
                ProjectWithContext(
                    project_id=str(r["project_id"]),
                    name=r["project_name"],
                    jurisdiction_id=str(r["jurisdiction_id"]),
                    occupancy_type=r.get("project_occupancy_type"),
                    metadata=(r.get("project_metadata") or {}),
                    design=DesignAttributes.from_snapshot_payload(payload),
                )
            )
    except psycopg.Error as exc:
        errors.append(f"retrieve: projects load failed: {exc}")

    print(
        f"[retrieve] jurisdiction={rule_change.jurisdiction!r} "
        f"({jurisdiction_id or 'unknown'}), "
        f"retrieved_rules={len(retrieved_rules)}, projects={len(matched_projects)}"
    )

    return {
        "jurisdiction_id": jurisdiction_id,
        "retrieved_rules": retrieved_rules,
        "matched_projects": matched_projects,
        "errors": list(state.get("errors", [])) + errors,
    }


# ---------------------------------------------------------------------------
# 2. classify_node  (LLM with retry + graceful degradation)
# ---------------------------------------------------------------------------
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _classify_with_llm(rule_change: RuleChange) -> ClassificationOutput:
    """Run the LLM classify call. Wrapped so tenacity can retry it."""
    llm = get_chat_model(temperature=0)
    structured = llm.with_structured_output(ClassificationOutput)
    prompt = CLASSIFY_PROMPT.format(
        jurisdiction=rule_change.jurisdiction,
        code_section=rule_change.code_section,
        old_text=rule_change.old_text or "(none — this appears to be a new rule)",
        new_text=rule_change.new_text,
        effective_date=str(rule_change.effective_date or "unknown"),
    )
    return ClassificationOutput.model_validate(structured.invoke(prompt))


def classify_node(state: GraphState) -> GraphState:
    """Classify the change type + plausible affected attributes (LLM)."""
    errors: list[str] = list(state.get("errors", []))
    rule_change: RuleChange = state["rule_change"]
    classification: Optional[ClassificationOutput] = None

    settings = get_settings()
    provider = (settings.LLM_PROVIDER or "").lower()
    key_ok = False
    if provider == "gemini":
        key_ok = bool(settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.strip())
    elif provider == "groq":
        key_ok = bool(settings.GROQ_API_KEY and settings.GROQ_API_KEY.strip())
    elif provider == "openrouter":
        key_ok = bool(settings.OPENROUTER_API_KEY and settings.OPENROUTER_API_KEY.strip())
    elif provider == "ollama":
        key_ok = True  # no key needed
    else:
        key_ok = False

    if not key_ok:
        msg = (
            f"[classify] WARNING: LLM provider={provider!r} has no valid key; "
            "skipping classification. Deterministic numeric checks will still run."
        )
        print(msg)
        errors.append("classify: LLM key missing — skipped (fallback to default classification)")
        classification = ClassificationOutput(
            change_type="amended",
            affected_attributes=[],
            rationale="Classification LLM skipped; treating as generic amendment.",
        )
    else:
        try:
            classification = _classify_with_llm(rule_change)
            print(
                f"[classify] type={classification.change_type!r}, "
                f"affected={classification.affected_attributes}, "
                f"rationale={classification.rationale!r}"
            )
        except Exception as exc:
            msg = (
                f"[classify] WARNING: LLM failed after retries: {exc!r}. "
                "Falling back to generic classification — deterministic checks still run."
            )
            print(msg)
            errors.append(f"classify: LLM failed ({exc!r}); using fallback.")
            classification = ClassificationOutput(
                change_type="amended",
                affected_attributes=[],
                rationale=f"LLM failed ({exc}); generic fallback.",
            )

    # Propagate the change_type back into the rule_change for downstream use.
    rule_change.change_type = classification.change_type

    return {
        "classification": classification,
        "rule_change": rule_change,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 3a. Deterministic numeric impact analyser
# ---------------------------------------------------------------------------
def _extract_numeric_thresholds(text: str) -> dict[str, Optional[float]]:
    """Return {attribute: threshold_value_or_None} for every numeric pattern we recognise.

    For each attribute we keep the MAX numeric match because regulations typically
    specify MINIMUM required values (e.g. "at least 2 hours") — so the biggest
    number in the rule is the binding constraint.
    """
    out: dict[str, Optional[float]] = {a: None for a, _ in _NUM_PATTERNS}
    for attr, pattern in _NUM_PATTERNS:
        nums: list[float] = []
        for m in pattern.finditer(text):
            try:
                nums.append(float(m.group("num")))
            except (ValueError, TypeError):
                continue
        if nums:
            out[attr] = max(nums)
    return out


def _cmp_deterministic(
    attr: str,
    design_value: Optional[float],
    threshold: float,
) -> Optional[tuple[FindingStatus, float, str]]:
    """Compare a project's design value against a rule threshold.

    Returns (status, confidence, short_explanation) or None if the comparison
    can't be made (e.g. design_value is missing).
    """
    if design_value is None:
        return None

    # Threshold semantics: regulations state minimums (>=) for fire / stories /
    # occupants and minimums (>=) for setbacks too (can't be closer than X ft,
    # so setback_ft in design must be >= threshold).
    status: FindingStatus
    try:
        dv = float(design_value)
    except (TypeError, ValueError):
        return None

    passes = dv >= threshold
    status = "compliant" if passes else "flagged"
    confidence = 0.95 if passes else 0.92  # deterministic → high confidence
    direction = ">=" if passes else "<"
    snippet = (
        f"{attr}: design={dv} {direction} required {threshold}. "
        f"{'Passes' if passes else 'FAILS'} deterministic numeric check."
    )
    return status, confidence, snippet


# ---------------------------------------------------------------------------
# 3b. LLM fallback impact analyser (confidence capped at 0.75)
# ---------------------------------------------------------------------------
LLM_FALLBACK_CONFIDENCE_CAP = 0.75


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _impact_with_llm(
    rule_change: RuleChange,
    project: ProjectWithContext,
) -> ComplianceFinding:
    """LLM-path impact analysis. Result confidence is ALWAYS <= 0.75."""
    llm = get_chat_model(temperature=0)
    structured = llm.with_structured_output(ComplianceFinding)
    prompt = IMPACT_LLM_PROMPT.format(
        code_section=rule_change.code_section,
        old_text=rule_change.old_text or "(no previous version)",
        new_text=rule_change.new_text,
        jurisdiction=rule_change.jurisdiction,
        source_url=rule_change.source_url,
        project_name=project.name,
        project_id=project.project_id,
        occupancy_type=project.occupancy_type or "unknown",
        project_metadata=json.dumps(project.metadata, indent=2, default=str),
        design_attrs=json.dumps(project.design.model_dump(mode="json"), indent=2),
        cap=LLM_FALLBACK_CONFIDENCE_CAP,
        disclaimer=DISCLAIMER_SENTENCE,
    )
    finding = ComplianceFinding.model_validate(structured.invoke(prompt))
    # Enforce the LLM-path confidence cap regardless of what the model claimed.
    if finding.confidence > LLM_FALLBACK_CONFIDENCE_CAP:
        finding.confidence = LLM_FALLBACK_CONFIDENCE_CAP
    # Ensure project/source identity matches the inputs (LLM can't change them).
    finding.project_id = project.project_id
    finding.source_url = rule_change.source_url
    # Pydantic validator auto-appends the disclaimer — we still force it via .model_validate.
    return finding


# ---------------------------------------------------------------------------
# 3. impact_node — hybrid (deterministic first, LLM fallback)
# ---------------------------------------------------------------------------
def impact_node(state: GraphState) -> GraphState:
    """Run deterministic checks for every project/attribute pair with data.

    Persist each finding to compliance_findings (insert_rule_change first so
    FK constraint is satisfied). LLM fallback only kicks in when NO
    deterministic check could be run for a given (project, rule) pair AND
    an LLM key is available.
    """
    errors: list[str] = list(state.get("errors", []))
    rule_change: RuleChange = state["rule_change"]
    projects: list[ProjectWithContext] = state.get("matched_projects", [])
    classification: Optional[ClassificationOutput] = state.get("classification")

    affected: set[str] = set()
    if classification and classification.affected_attributes:
        affected = set(classification.affected_attributes) & DETERMINISTIC_ATTRIBUTES
    if not affected:
        affected = set(DETERMINISTIC_ATTRIBUTES)  # try them all if classifier omitted

    # Extract thresholds preferentially from new_text (and fall back to old_text
    # so we can still do numeric checks on repealed / clarified rules).
    thresholds = _extract_numeric_thresholds(rule_change.new_text)
    if rule_change.old_text:
        old_thresholds = _extract_numeric_thresholds(rule_change.old_text)
        # Use the larger of new / old for each attr (treat the stricter rule as active).
        for k in thresholds:
            if old_thresholds[k] is not None and (
                thresholds[k] is None or old_thresholds[k] > thresholds[k]
            ):
                thresholds[k] = old_thresholds[k]

    print(
        f"[impact] extracted numeric thresholds: "
        + ", ".join(f"{k}={v}" for k, v in thresholds.items() if v is not None)
        + " (from new_text + old_text)"
    )

    # Persist a single rule_changes row that all compliance_findings can point at.
    rule_change_id: Optional[str] = None
    try:
        retrieved = state.get("retrieved_rules", [])
        matched_rule_id = retrieved[0]["id"] if retrieved else None
        rule_change_id = insert_rule_change(
            rule_id=matched_rule_id,
            change_type=rule_change.change_type or "amended",
            old_text=rule_change.old_text,
            new_text=rule_change.new_text,
        )
    except psycopg.Error as exc:
        errors.append(f"impact: insert_rule_change failed: {exc}")

    findings: list[ComplianceFinding] = []
    settings = get_settings()
    llm_available = bool(
        settings.GEMINI_API_KEY
        or settings.GROQ_API_KEY
        or settings.OPENROUTER_API_KEY
        or settings.LLM_PROVIDER.lower() == "ollama"
    )

    for project in projects:
        project_determined_any = False
        for attr in sorted(affected):
            threshold = thresholds.get(attr)
            if threshold is None:
                continue
            design_val = getattr(project.design, attr, None)
            cmp_result = _cmp_deterministic(attr, design_val, threshold)
            if cmp_result is None:
                continue
            status, conf, snippet = cmp_result
            project_determined_any = True
            direction = "increased" if (rule_change.old_text and attr in _extract_numeric_thresholds(rule_change.old_text) and _extract_numeric_thresholds(rule_change.old_text)[attr] not in (None, threshold)) else "changed"
            action = (
                f"Raise {attr} to at least {threshold}."
                if status == "flagged"
                else f"Keep {attr} at current value ({design_val})."
            )
            rule_quote = (
                f"[{rule_change.code_section}] new='{rule_change.new_text[:240]}'"
                + (
                    f" | old='{rule_change.old_text[:240]}'"
                    if rule_change.old_text
                    else ""
                )
            )
            explanation = (
                f"Rule {rule_change.code_section} {direction}: "
                f"{snippet} Project: {project.name} (id={project.project_id[:8]}...). "
                f"Suggested action: {action} "
                f"Source: {rule_change.source_url}. {DISCLAIMER_SENTENCE}"
            )
            finding = ComplianceFinding(
                project_id=project.project_id,
                rule_change_id=rule_change_id,
                status=status,
                confidence=conf,
                explanation=explanation,
                cited_rule_text=rule_quote,
                source_url=rule_change.source_url,
                matched_attribute=attr,
            )
            try:
                insert_compliance_finding(
                    project_id=finding.project_id,
                    rule_change_id=finding.rule_change_id,
                    status=finding.status,
                    confidence=finding.confidence,
                    explanation=finding.explanation,
                )
                print(
                    f"[impact] DETERMINISTIC attr={attr} project={project.name!r} "
                    f"-> {status} (conf={conf})"
                )
            except psycopg.Error as exc:
                errors.append(
                    f"impact: insert_finding failed for {project.project_id}/{attr}: {exc}"
                )
            findings.append(finding)

        # LLM fallback — only when NO deterministic comparison ran for this project.
        if not project_determined_any and llm_available:
            try:
                lfinding = _impact_with_llm(rule_change, project)
                lfinding.rule_change_id = rule_change_id
                try:
                    insert_compliance_finding(
                        project_id=lfinding.project_id,
                        rule_change_id=lfinding.rule_change_id,
                        status=lfinding.status,
                        confidence=lfinding.confidence,
                        explanation=lfinding.explanation,
                    )
                    print(
                        f"[impact] LLM_FALLBACK project={project.name!r} "
                        f"-> {lfinding.status} (conf capped at {lfinding.confidence})"
                    )
                except psycopg.Error as exc:
                    errors.append(
                        f"impact: insert_llm_finding failed for {project.project_id}: {exc}"
                    )
                findings.append(lfinding)
            except Exception as exc:
                errors.append(
                    f"impact: LLM fallback failed for {project.project_id}: {exc!r}"
                )

    return {
        "findings": findings,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 4. alert_node — finalise ComplianceFinding objects (format checks + enrich)
# ---------------------------------------------------------------------------
def alert_node(state: GraphState) -> GraphState:
    """Final pass: ensure every finding has the canonical explanation shape.

    Explanation template:
      old rule → new rule → which attribute conflicts → suggested action →
      source_url → disclaimer
    """
    errors: list[str] = list(state.get("errors", []))
    rule_change: RuleChange = state["rule_change"]
    findings: list[ComplianceFinding] = list(state.get("findings", []))

    projects_by_id: dict[str, ProjectWithContext] = {
        p.project_id: p for p in state.get("matched_projects", [])
    }

    finalised: list[ComplianceFinding] = []
    for f in findings:
        try:
            project = projects_by_id.get(f.project_id)
            attr_note = (
                f"Conflicting design attribute: {f.matched_attribute}."
                if f.matched_attribute
                else "Attribute-level conflict not isolated."
            )
            action_hint = (
                "Remediate the flagged attribute and re-run the check."
                if f.status == "flagged"
                else (
                    "No action required."
                    if f.status == "compliant"
                    else "Await human review before acting."
                )
            )
            old = rule_change.old_text or "(new rule — no prior version)"
            full_explanation = (
                f"Old rule: {old[:300]} | "
                f"New rule: {rule_change.new_text[:300]} | "
                f"{attr_note} Suggested action: {action_hint} "
                f"Source: {f.source_url}. {DISCLAIMER_SENTENCE}"
            )
            finalised.append(
                ComplianceFinding(
                    project_id=f.project_id,
                    rule_change_id=f.rule_change_id,
                    status=f.status,
                    confidence=f.confidence,
                    explanation=full_explanation,
                    cited_rule_text=f.cited_rule_text
                    or (
                        f"OLD: {old[:240]}  /  NEW: {rule_change.new_text[:240]}"
                    ),
                    source_url=f.source_url,
                    matched_attribute=f.matched_attribute,
                )
            )
        except Exception as exc:
            errors.append(f"alert: enrich finding failed ({f.project_id}): {exc!r}")
            finalised.append(f)  # keep the original rather than losing it

    print(f"[alert] finalised {len(finalised)} findings")
    return {
        "findings": finalised,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 5. human_review_gate — conditional edge router
# ---------------------------------------------------------------------------
def human_review_gate(state: GraphState) -> str:
    """Conditional edge. Returns either "notify" or "needs_review".

    Routing rule:
      - If ANY finding has status == 'flagged' AND confidence >= threshold,
        route through "notify" (auto-raise compliance.flag.raised).
      - Everything else → needs_review queue.

    Also populates notify_project_ids / needs_review_project_ids on the state
    so downstream callers (Inngest dispatchers, API handlers) can act on them
    without re-examining the full findings list.
    """
    findings: list[ComplianceFinding] = state.get("findings", [])
    notify_ids: list[str] = []
    review_ids: list[str] = []

    for f in findings:
        high_conf_flag = (
            f.status == "flagged" and f.confidence >= AUTO_FLAG_CONFIDENCE_THRESHOLD
        )
        if high_conf_flag:
            notify_ids.append(f.project_id)
        else:
            review_ids.append(f.project_id)

    verdict = "notify" if notify_ids else "needs_review"

    print(
        f"[gate] threshold={AUTO_FLAG_CONFIDENCE_THRESHOLD}, "
        f"notify={len(notify_ids)}, needs_review={len(review_ids)} -> {verdict.upper()}"
    )

    # Write the routed lists back into the state via a small trick: we can't
    # mutate state from the router fn directly because routers only return the
    # edge name, but the subsequent endpoints (which ARE nodes) are what read
    # the findings. We simply surface via printing here; the test harness and
    # callers read the findings themselves to decide.
    return verdict


def _notify_endpoint(state: GraphState) -> GraphState:
    """Terminal path: findings eligible for auto-raise.

    In production this node would emit a `compliance.flag.raised` Inngest event
    per project. For local runs and tests we just tag the state.
    """
    notify_ids = [
        f.project_id
        for f in state.get("findings", [])
        if f.status == "flagged" and f.confidence >= AUTO_FLAG_CONFIDENCE_THRESHOLD
    ]
    review_ids = [
        f.project_id
        for f in state.get("findings", [])
        if not (f.status == "flagged" and f.confidence >= AUTO_FLAG_CONFIDENCE_THRESHOLD)
    ]
    return {
        "notify_project_ids": notify_ids,
        "needs_review_project_ids": review_ids,
        "errors": list(state.get("errors", [])),
    }


def _needs_review_endpoint(state: GraphState) -> GraphState:
    """Terminal path: findings queued for human review."""
    review_ids = [f.project_id for f in state.get("findings", [])]
    return {
        "notify_project_ids": [],
        "needs_review_project_ids": review_ids,
        "errors": list(state.get("errors", [])),
    }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
def build_graph() -> StateGraph:
    """Construct the compliance-check StateGraph (call .compile() on result)."""
    g = StateGraph(GraphState)

    g.add_node("retrieve", retrieve_node)
    g.add_node("classify", classify_node)
    g.add_node("impact", impact_node)
    g.add_node("alert", alert_node)
    g.add_node("notify", _notify_endpoint)
    g.add_node("needs_review", _needs_review_endpoint)

    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "classify")
    g.add_edge("classify", "impact")
    g.add_edge("impact", "alert")
    g.add_conditional_edges(
        "alert",
        human_review_gate,
        {
            "notify": "notify",
            "needs_review": "needs_review",
        },
    )
    g.add_edge("notify", END)
    g.add_edge("needs_review", END)
    return g


def run_graph(rule_change: RuleChange) -> GraphState:
    """Convenience helper: compile the graph, run it, return the final state."""
    workflow = build_graph().compile()
    initial: GraphState = {"rule_change": rule_change, "errors": []}
    result: GraphState = workflow.invoke(initial)
    return result


# ---------------------------------------------------------------------------
# API-friendly helpers (reuse nodes — no duplicated logic)
# ---------------------------------------------------------------------------
def rule_change_from_persisted_row(row: dict[str, Any]) -> RuleChange:
    """Build a RuleChange from the dict returned by db.get_rule_change_by_id()."""
    jurisdiction_name = row.get("jurisdiction_name") or "Unknown"
    return RuleChange(
        jurisdiction=jurisdiction_name,
        code_section=row.get("code_section") or f"rule_change:{row['rule_change_id']}",
        old_text=row.get("old_text"),
        new_text=row.get("new_text") or "",
        effective_date=row.get("effective_date"),
        source_url=row.get("source_url") or "",
        change_type=row.get("change_type"),  # type: ignore[arg-type]
    )


def run_check_for_rule_change_id(
    rule_change_id: str,
) -> tuple[GraphState, list[ComplianceFinding]]:
    """POST /compliance-check mode (a): run the graph for a persisted rule_change_id.

    Returns (final_graph_state, list_of_findings_as_pydantic_models).
    Raises KeyError if the rule_change_id is unknown.
    """
    from agent.db import get_rule_change_by_id  # local import to avoid cycles

    row = get_rule_change_by_id(rule_change_id)
    if row is None:
        raise KeyError(f"rule_change_id {rule_change_id!r} not found")
    rule_change = rule_change_from_persisted_row(row)
    state = run_graph(rule_change)
    findings: list[ComplianceFinding] = list(state.get("findings", []) or [])
    return state, findings


def run_check_for_project_id(
    project_id: str,
) -> tuple[GraphState, list[ComplianceFinding]]:
    """POST /compliance-check mode (b): re-check ONE project against ALL its
    jurisdiction's current rules.

    Strategy — reuse the existing graph/nodes rather than duplicating logic:
      1. Load project + its jurisdiction from DB.
      2. Load all current rules for that jurisdiction (up to 500 — reasonable
         for a demo; pagination would be trivial to add later).
      3. For every rule, construct a synthetic RuleChange that represents the
         rule in its CURRENT state (old_text=None, new_text=<rule.text>,
         change_type=None / "clarification" on first pass).
      4. Run retrieve/classify/impact/alert via run_graph() once per rule.
      5. To avoid comparing unrelated projects against unrelated rules, the
         retrieve_node already filters projects by jurisdiction_id, and we
         deduplicate findings downstream by (project_id, rule.code_section).

    Returns (a merged GraphState with merged findings/errors, findings list).
    """
    from agent.db import (  # local import to avoid cycles
        get_project_by_id,
        list_rules_for_jurisdiction,
    )

    project = get_project_by_id(project_id)
    if project is None:
        raise KeyError(f"project_id {project_id!r} not found")
    jurisdiction_id = project["jurisdiction_id"]
    jurisdiction_name = project["jurisdiction_name"]

    rules = list_rules_for_jurisdiction(jurisdiction_id, limit=500)
    if not rules:
        # No rules for this jurisdiction yet → return empty state.
        empty_state: GraphState = {
            "rule_change": RuleChange(
                jurisdiction=jurisdiction_name,
                code_section="(no rules for jurisdiction)",
                new_text="",
                source_url="",
            ),
            "matched_projects": [],
            "findings": [],
            "errors": [f"No rules found for jurisdiction {jurisdiction_name!r}"],
        }
        return empty_state, []

    all_findings: list[ComplianceFinding] = []
    all_errors: list[str] = []
    seen_keys: set[tuple[str, str]] = set()
    merged_state: GraphState = {}

    for rule in rules:
        synthetic = RuleChange(
            jurisdiction=jurisdiction_name,
            code_section=rule.get("code_section") or f"rule:{rule['id']}",
            old_text=None,
            new_text=rule.get("text") or "",
            effective_date=rule.get("effective_date"),  # type: ignore[arg-type]
            source_url=rule.get("source_url") or "",
            change_type=None,
        )
        state = run_graph(synthetic)
        # Keep only findings for the specific project_id the user asked about.
        for f in list(state.get("findings", []) or []):
            if f.project_id != project_id:
                continue
            dedupe_key = (f.project_id, f.matched_attribute or "none", synthetic.code_section)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            all_findings.append(f)
        for e in state.get("errors", []) or []:
            all_errors.append(e)

    merged_state["findings"] = all_findings
    merged_state["errors"] = all_errors
    return merged_state, all_findings
