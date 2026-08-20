"""PRCA Agent FastAPI service — exposes the compliance-check LangGraph.

Endpoints:
  POST /compliance-check   — run the full graph (mode a: rule_change_id OR mode b: project_id)
  POST /ask                — RAG Q&A over rules / rule_changes / findings with cited sources
  GET  /findings           — list compliance_findings, filterable
  GET  /health             — liveness + DB connectivity

Cross-cutting:
  - CORS for http://localhost:3000 (Next.js dev server)
  - Per-request stdout logging: path + duration_ms
  - Structured error responses: unknown IDs → 404, LLM provider errors → 502
  - Pydantic request/response models (clean OpenAPI at /docs)
  - Auth dependency placeholder (`_auth_dependency_placeholder`) — injectable into
    all routes in a single place later, in Step 10.
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Optional

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from agent.config import get_settings  # noqa: E402
from agent.db import (  # noqa: E402
    get_connection,
    get_jurisdiction_by_name,
    get_rule_change_detail,
    ingest_rule_change,
    list_findings,
    list_projects,
    list_recent_findings,
    list_recent_rule_changes,
    search_rules,
    upsert_compliance_finding,
)
from agent.embeddings import embed  # noqa: E402
from agent.graph import (  # noqa: E402
    run_check_for_project_id,
    run_check_for_rule_change_id,
)
from agent.llm import get_chat_model  # noqa: E402
from agent.models import (  # noqa: E402
    ComplianceFinding,
    DISCLAIMER_SENTENCE,
    FindingStatus,
)

# ---------------------------------------------------------------------------
# App bootstrap + middleware
# ---------------------------------------------------------------------------
settings = get_settings()

app = FastAPI(
    title="PRCA Agent Service",
    description=(
        "Permitting & Regulatory Compliance Agent — LangGraph compliance checks, "
        "rules RAG (/ask), and audit-logs (/findings). OpenAPI at /docs."
    ),
    version="0.2.0",
)

# CORS — allow Next.js dev server + same-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _log_request_duration(request: Request, call_next: Any) -> Any:
    """Log every request's path + status + duration_ms to stdout."""
    started = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        status = 500
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        print(
            f"[http] {request.method:6s} {request.url.path:32s} -> {status} "
            f"({duration_ms} ms)"
        )
    return response


# Auth placeholder — in Step 10, replace this with a real Depends(verify_jwt_or_session)
# and add the dependency to every endpoint's signature in one edit.
async def _auth_dependency_placeholder() -> None:  # pragma: no cover - no-op today
    return None


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------
class ComplianceCheckRequest(BaseModel):
    rule_change_id: Optional[str] = Field(
        default=None, description="Run the graph for this persisted rule_change."
    )
    project_id: Optional[str] = Field(
        default=None, description="Re-check this project against ALL of its jurisdiction's rules."
    )

    @field_validator("rule_change_id", "project_id")
    @classmethod
    def _uuidish(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Accept any non-empty string; the DB layer validates existence anyway.
        s = v.strip()
        if not s:
            raise ValueError("must not be empty")
        return s

    @field_validator("project_id")
    @classmethod
    def _exactly_one(cls, v: Optional[str], info: Any) -> Optional[str]:
        other = info.data.get("rule_change_id")
        if v and other:
            raise ValueError("supply EXACTLY ONE of rule_change_id or project_id (not both)")
        if v is None and other is None:
            raise ValueError("supply exactly ONE of rule_change_id or project_id")
        return v


class FindingOut(BaseModel):
    """Response shape for compliance_findings (matches ComplianceFinding fields + IDs)."""

    id: Optional[str] = None
    project_id: str
    project_name: Optional[str] = None
    rule_change_id: Optional[str] = None
    rule_code_section: Optional[str] = None
    rule_jurisdiction: Optional[str] = None
    status: FindingStatus
    confidence: float = Field(..., ge=0, le=1)
    explanation: str
    cited_rule_text: Optional[str] = None
    source_url: Optional[str] = None
    matched_attribute: Optional[str] = None
    created_at: Optional[datetime] = None


class ComplianceCheckResponse(BaseModel):
    run_id: str
    duration_ms: int
    mode: str
    findings: list[FindingOut]


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, description="The user's free-text question.")
    jurisdiction: Optional[str] = Field(
        default=None,
        description="Optional jurisdiction name to narrow retrieval. Empty = search all.",
    )


class AskSource(BaseModel):
    code_section: str
    source_url: str


class AskResponse(BaseModel):
    answer: str
    sources: list[AskSource]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    db: Optional[Literal["ok", "down"]] = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)


class ErrorEnvelope(BaseModel):
    """Structured error body — never includes stack traces or env values."""

    error: str
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# 5. POST /ingest-rule-change request / response models
# ---------------------------------------------------------------------------
class IngestRuleChangeRequest(BaseModel):
    """Payload for POST /ingest-rule-change — comes from the Inngest poller.

    Each payload represents ONE detected regulatory rule change. This endpoint
    inserts the rule (if new/updated) and a rule_change row, then returns the
    rule_change_id so the caller can emit "regulatory.change.detected".

    change_type may be omitted — the classify_node will run and override it
    during compliance checks anyway. We accept it optionally so the poller can
    hint if a feed labels a change explicitly.
    """

    jurisdiction: str = Field(..., min_length=1, description="Jurisdiction name.")
    code_section: str = Field(..., min_length=1, description="Code section identifier.")
    old_text: Optional[str] = Field(
        default=None,
        description="Previous rule text (None for brand-new rules).",
    )
    new_text: str = Field(..., min_length=1, description="Current/amended rule text.")
    effective_date: Optional[date] = Field(
        default=None, description="YYYY-MM-DD effective date, if known."
    )
    source_url: str = Field(..., min_length=1, description="Authoritative source URL.")
    change_type: Optional[Literal["new", "amended", "repealed", "clarification"]] = Field(
        default=None, description="Optional pre-classified change type."
    )


class IngestRuleChangeResponse(BaseModel):
    rule_change_id: str
    rule_id: str
    jurisdiction_id: str
    change_type: Literal["new", "amended", "repealed", "clarification"]
    ingested_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Helpers used by the endpoints
# ---------------------------------------------------------------------------
def _finding_to_response(
    f: ComplianceFinding | dict[str, Any],
    *,
    project_lookup: dict[str, str] | None = None,
) -> FindingOut:
    """Convert a ComplianceFinding Pydantic model OR a raw db row dict into FindingOut."""
    if isinstance(f, ComplianceFinding):
        out = FindingOut(
            project_id=f.project_id,
            rule_change_id=f.rule_change_id,
            status=f.status,
            confidence=float(f.confidence),
            explanation=f.explanation,
            cited_rule_text=f.cited_rule_text,
            source_url=f.source_url,
            matched_attribute=f.matched_attribute,
        )
        if project_lookup:
            out.project_name = project_lookup.get(f.project_id)
        return out
    # Raw DB row (from list_findings)
    pid = str(f.get("project_id"))
    return FindingOut(
        id=str(f["id"]) if f.get("id") else None,
        project_id=pid,
        project_name=f.get("project_name") or (project_lookup or {}).get(pid),
        rule_change_id=str(f["rule_change_id"]) if f.get("rule_change_id") else None,
        rule_code_section=f.get("rule_code_section"),
        rule_jurisdiction=f.get("rule_jurisdiction"),
        status=f["status"],  # type: ignore[arg-type]
        confidence=float(f.get("confidence") or 0.0),
        explanation=f.get("explanation") or "",
        created_at=f.get("created_at"),  # type: ignore[arg-type]
    )


def _persist_findings_idempotent(findings: list[ComplianceFinding]) -> None:
    """Write each finding to compliance_findings (upsert — no duplicates)."""
    for f in findings:
        try:
            upsert_compliance_finding(
                project_id=f.project_id,
                rule_change_id=f.rule_change_id,
                status=f.status,
                confidence=float(f.confidence),
                explanation=f.explanation,
            )
        except psycopg.Error as exc:
            # Non-fatal for the API caller — the in-memory findings are still returned.
            # Surface in server logs so it's not silent.
            print(f"[warn] upsert_compliance_finding failed for {f.project_id}: {exc}")


def _build_project_lookup() -> dict[str, str]:
    """Map project_id -> project_name for enriching response output."""
    out: dict[str, str] = {}
    try:
        with get_connection() as conn:
            cur = conn.execute("SELECT id, name FROM projects;")
            for r in cur.fetchall():
                out[str(r["id"])] = r["name"]
    except psycopg.Error:
        pass
    return out


# ---------------------------------------------------------------------------
# 1. POST /compliance-check
# ---------------------------------------------------------------------------
@app.post(
    "/compliance-check",
    response_model=ComplianceCheckResponse,
    responses={
        404: {"model": ErrorEnvelope, "description": "rule_change_id / project_id not found"},
        500: {"model": ErrorEnvelope, "description": "DB or unexpected server error"},
        502: {"model": ErrorEnvelope, "description": "LLM provider error (graph degraded gracefully)"},
    },
)
def compliance_check(body: ComplianceCheckRequest) -> ComplianceCheckResponse:
    """Run the PRCA compliance-check agent graph.

    Supply **exactly one** of:
      - `rule_change_id` → run the graph against a persisted rule change (mode a)
      - `project_id` → re-check ONE project against ALL current rules for its
        jurisdiction (mode b — constructs synthetic RuleChanges and reuses
        the same nodes; no duplicated logic).
    """
    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    llm_error = False

    try:
        if body.rule_change_id:
            state, findings = run_check_for_rule_change_id(body.rule_change_id)
            mode = "rule_change_id"
        else:
            assert body.project_id is not None
            state, findings = run_check_for_project_id(body.project_id)
            mode = "project_id"
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "detail": str(exc)})
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "database_error", "detail": f"DB error: {exc.__class__.__name__}"},
        )
    except Exception as exc:
        # LLM / provider errors don't leak internal details; 502 signals an upstream dependency.
        msg = str(exc)
        llm_keywords = ("llm", "gemini", "groq", "openrouter", "ollama", "api key", "rate limit")
        if any(k in msg.lower() for k in llm_keywords):
            llm_error = True
        elif any(kw in type(exc).__name__.lower() for kw in llm_keywords):
            llm_error = True

        if llm_error:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "llm_provider_error",
                    "detail": (
                        "LLM provider failed; deterministic checks ran with available data. "
                        "Retry in a moment, or set LLM_PROVIDER=ollama for fully local operation."
                    ),
                },
            )
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "detail": "An unexpected error occurred."},
        )

    # Persist findings idempotently (optional DB write; response already finalised from memory).
    _persist_findings_idempotent(findings)

    project_lookup = _build_project_lookup()
    findings_out = [_finding_to_response(f, project_lookup=project_lookup) for f in findings]
    duration_ms = int((time.perf_counter() - started) * 1000)

    return ComplianceCheckResponse(
        run_id=run_id,
        duration_ms=duration_ms,
        mode=mode,
        findings=findings_out,
    )


# ---------------------------------------------------------------------------
# 2. POST /ask  — RAG Q&A with LLM using ONLY retrieved context
# ---------------------------------------------------------------------------
ASK_PROMPT = """\
You are a PRCA (Permitting & Regulatory Compliance Agent) assistant answering a
user's question about building / zoning rules.

STRICT GROUND RULES — obey these or you will fail:
1. Answer ONLY using the RETRIEVED CONTEXT provided below. Do NOT use any outside
   knowledge, training data facts, or guesses.
2. If the retrieved context does not cover the user's question — EITHER because
   the jurisdiction isn't in the context or the topic isn't there — you MUST
   explicitly say so. The canonical phrasing is:
     "I don't have data for that jurisdiction / topic in my regulatory dataset."
   Do NOT say "maybe", do NOT paraphrase plausible rules, do NOT hedge.
3. Every concrete claim you make about a rule MUST include its source_url. Format:
     - Claim sentence. ([Code Section](source_url))
4. You must NOT state or imply legal certainty. Whenever you cite a rule, append
   a one-line disclaimer:
     "Note: Flagged for review — not a legal compliance determination."
   once at the END of your answer.
5. If multiple sources are relevant, cite the 2–4 strongest matches. Don't
   list irrelevant sources just because they were retrieved.

USER QUESTION:
{question}

RETRIEVED CONTEXT — use only these snippets to answer. Each has a code_section,
jurisdiction, source_url, and body text:
{context_block}

Now write your answer. Start with a direct reply, then inline-cite each claim,
then list any sources separately only if helpful. If the context is insufficient,
state exactly that using the required phrasing and nothing else.
"""


def _ask_retrieve_context(question: str, jurisdiction_name: Optional[str]) -> tuple[list[dict[str, Any]], list[AskSource]]:
    """Embed the question → pgvector search rules + recent rule_changes + findings.

    Returns (context_dicts, sources_list).
    """
    jurisdiction_id: Optional[str] = None
    if jurisdiction_name:
        j = get_jurisdiction_by_name(jurisdiction_name)
        if j:
            jurisdiction_id = j["id"]

    # 1. pgvector rules search (top 8).
    rule_hits: list[dict[str, Any]] = []
    try:
        qvec = embed([question])
        if qvec:
            rule_hits = search_rules(qvec[0], jurisdiction_id=jurisdiction_id, limit=8)
    except Exception as exc:
        print(f"[warn] /ask rule search failed: {exc!r}")

    # 2. Recent rule_changes (up to 6) — just append as context.
    change_hits: list[dict[str, Any]] = []
    try:
        change_hits = list_recent_rule_changes(limit=6)
    except psycopg.Error as exc:
        print(f"[warn] /ask rule_changes fetch failed: {exc}")

    # 3. Recent findings (up to 5) — append as case-resolution context.
    finding_hits: list[dict[str, Any]] = []
    try:
        finding_hits = list_recent_findings(limit=5)
    except psycopg.Error as exc:
        print(f"[warn] /ask findings fetch failed: {exc}")

    context_dicts: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str]] = set()
    sources: list[AskSource] = []

    def _add_source(code: str, url: str) -> None:
        code_s = (code or "").strip() or "(untitled)"
        url_s = (url or "").strip()
        key = (code_s, url_s)
        if key in seen_sources:
            return
        seen_sources.add(key)
        sources.append(AskSource(code_section=code_s, source_url=url_s))

    for idx, r in enumerate(rule_hits):
        body = r.get("text") or ""
        code = r.get("code_section") or f"rule-{idx}"
        url = r.get("source_url") or ""
        if jurisdiction_name:
            context_dicts.append({
                "kind": "rule",
                "jurisdiction": jurisdiction_name,
                "code_section": code,
                "source_url": url,
                "body": body[:1500],
            })
        else:
            # Look up jurisdiction name from rules.jurisdiction_id
            jname = "Unknown"
            try:
                with get_connection() as conn:
                    cur = conn.execute(
                        "SELECT name FROM jurisdictions WHERE id = %s LIMIT 1;",
                        (r["jurisdiction_id"],),
                    )
                    row = cur.fetchone()
                    if row:
                        jname = row["name"]
            except psycopg.Error:
                pass
            context_dicts.append({
                "kind": "rule",
                "jurisdiction": jname,
                "code_section": code,
                "source_url": url,
                "body": body[:1500],
            })
        _add_source(code, url)

    for rc in change_hits:
        code = rc.get("code_section") or f"rule_change:{rc['id']}"
        url = rc.get("source_url") or ""
        body = f"CHANGE ({rc.get('change_type')}): {rc.get('new_text') or ''}"
        if rc.get("old_text"):
            body += f" | OLD: {rc.get('old_text')}"
        context_dicts.append({
            "kind": "rule_change",
            "jurisdiction": rc.get("jurisdiction") or "Unknown",
            "code_section": code,
            "source_url": url,
            "body": body[:1500],
        })
        _add_source(code, url)

    for cf in finding_hits:
        code = f"finding:{cf['id'][:8]}"
        url = ""
        body = (
            f"FINDING status={cf['status']} conf={cf['confidence']} "
            f"project={cf.get('project_name') or cf['project_id']}: "
            f"{cf.get('explanation') or ''}"
        )
        context_dicts.append({
            "kind": "compliance_finding",
            "jurisdiction": "N/A",
            "code_section": code,
            "source_url": url,
            "body": body[:1500],
        })

    return context_dicts, sources


def _format_context_block(ctx: list[dict[str, Any]]) -> str:
    lines = []
    for i, c in enumerate(ctx, start=1):
        lines.append(
            f"[{i}] kind={c['kind']}  jurisdiction={c['jurisdiction']!r}  "
            f"code_section={c['code_section']!r}  source_url={c['source_url']!r}\n"
            f"    {c['body'].strip()}"
        )
    if not lines:
        lines.append("[NO CONTEXT RETRIEVED — dataset is empty]")
    return "\n\n".join(lines)


@app.post(
    "/ask",
    response_model=AskResponse,
    responses={
        502: {"model": ErrorEnvelope, "description": "LLM provider unavailable"},
    },
)
def ask(body: AskRequest) -> AskResponse:
    """Answer a regulatory question ONLY using retrieved rules + changes + findings.

    - Embeds the question → pgvector similarity search (top 8 rules).
    - Also appends recent rule_changes + recent compliance_findings as context.
    - Calls the LLM with a strict prompt that FORBIDS using any outside knowledge
      and REQUIRES saying "I don't have data for that jurisdiction/topic..." if
      the context doesn't cover the question.
    - Every concrete claim cites its source_url.
    """
    question = body.question.strip()
    ctx, sources = _ask_retrieve_context(question, body.jurisdiction)

    # If the user asked for a specific jurisdiction but retrieval returned ZERO
    # hits tagged with that jurisdiction, short-circuit BEFORE the LLM call —
    # say so explicitly (no hallucination risk).
    if body.jurisdiction:
        relevant = [c for c in ctx if c.get("jurisdiction") == body.jurisdiction]
        if not relevant:
            return AskResponse(
                answer=(
                    f"I don't have data for that jurisdiction / topic in my "
                    f"regulatory dataset. (Requested jurisdiction: {body.jurisdiction!r}; "
                    f"no rules were retrieved for it.)"
                ),
                sources=[],
            )

    context_block = _format_context_block(ctx)
    prompt = ASK_PROMPT.format(question=question, context_block=context_block)

    try:
        llm = get_chat_model(temperature=0)
        answer_raw = llm.invoke(prompt).content
    except Exception as exc:
        # LLM not available — answer with retrieval-only summary + the exact
        # "don't have data" fallback phrasing, and return 502 so callers see the
        # upstream dependency failed.
        llm_err = (
            "LLM provider unavailable. Retrieved context snippets summary:\n"
            + "\n".join(
                f"  - {c['jurisdiction']}: [{c['code_section']}] "
                f"{c['body'][:120]}...  ({c['source_url']})"
                for c in ctx[:5]
            )
            + f"\n\nI don't have data for that jurisdiction / topic in my regulatory dataset.\n\n"
            f"{DISCLAIMER_SENTENCE}"
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "llm_provider_error",
                "detail": f"LLM call failed: {exc.__class__.__name__}. Retrieved {len(ctx)} context snippets.",
                "fallback_answer": llm_err,
            },
        )

    if isinstance(answer_raw, str):
        answer = answer_raw.strip()
    else:
        # Some providers return AIMessage-like objects; fall back to str cast.
        answer = str(answer_raw).strip()

    # Lightweight sanity check: if the LLM produced an empty answer or one that
    # looks like it admitted no coverage, guarantee the disclaimer + phrasing.
    if not answer:
        answer = (
            "I don't have data for that jurisdiction / topic in my "
            f"regulatory dataset. {DISCLAIMER_SENTENCE}"
        )

    # Guarantee the disclaimer sentence once at the end (Pydantic validator only
    # applies to ComplianceFinding models — this is free-text so enforce it manually).
    if DISCLAIMER_SENTENCE not in answer:
        if not answer.endswith((".", "!", "?")):
            answer += "."
        answer += " " + DISCLAIMER_SENTENCE

    return AskResponse(answer=answer, sources=sources)


# ---------------------------------------------------------------------------
# 3. GET /findings
# ---------------------------------------------------------------------------
@app.get("/findings", response_model=list[FindingOut])
def list_findings_endpoint(
    project_id: Optional[str] = Query(default=None, description="Filter by project UUID."),
    status: Optional[FindingStatus] = Query(default=None, description="Filter by finding status."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[FindingOut]:
    """List compliance_findings, most-recent-first, filterable.

    Powers the audit-log view and project-level finding lists in the UI.
    """
    try:
        rows = list_findings(
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "database_error", "detail": f"DB error: {exc.__class__.__name__}"},
        )
    return [_finding_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# 4. GET /health — extended with DB connectivity check
# ---------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness probe. Also checks DB connectivity."""
    db_status: Optional[Literal["ok", "down"]] = None
    status: Literal["ok", "degraded", "down"] = "ok"
    try:
        with get_connection() as conn:
            cur = conn.execute("SELECT 1;")
            cur.fetchone()
        db_status = "ok"
    except psycopg.Error:
        db_status = "down"
        status = "degraded"
    except Exception:
        db_status = "down"
        status = "degraded"
    return HealthResponse(status=status, db=db_status)


# ---------------------------------------------------------------------------
# 5. POST /ingest-rule-change — Inngest poller / external feed ingestion
# ---------------------------------------------------------------------------
@app.post(
    "/ingest-rule-change",
    response_model=IngestRuleChangeResponse,
    responses={
        400: {"model": ErrorEnvelope, "description": "Malformed payload (e.g. empty new_text)"},
        500: {"model": ErrorEnvelope, "description": "DB or embedding error"},
    },
)
def ingest_rule_change_endpoint(body: IngestRuleChangeRequest) -> IngestRuleChangeResponse:
    """Ingest a single rule change: embeds it, upserts rule + rule_change rows.

    **Idempotent**: retries with the same (jurisdiction, code_section, new_text)
    will return the existing rule_change_id rather than creating duplicates.

    Used by:
      - Inngest `pollRegulatorySources` (reads JSON files from /data/incoming/)
      - Future per-jurisdiction scrapers / API pollers (swap in behind the same event)

    After a successful ingest, the caller emits `regulatory.change.detected`
    with the returned `rule_change_id` to kick off the compliance-check graph.
    """
    new_text = body.new_text.strip()
    if not new_text:
        raise HTTPException(status_code=400, detail={"error": "bad_request", "detail": "new_text is empty"})
    if not body.source_url.strip():
        raise HTTPException(status_code=400, detail={"error": "bad_request", "detail": "source_url is empty"})

    # 1. Embed the new rule text (all-MiniLM-L6-v2, 384 dims, $0 local).
    try:
        vecs = embed([new_text])
        if not vecs or len(vecs[0]) != 384:
            raise RuntimeError("embed() returned empty or wrong-dim vector")
        embedding = vecs[0]
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "embedding_error", "detail": f"Failed to embed rule text: {exc.__class__.__name__}"},
        )

    # 2. Resolve change_type: use provided hint, else default "amended".
    #    (the classify_node will overwrite this during compliance-check anyway).
    change_type = body.change_type or ("new" if body.old_text is None else "amended")

    # 3. Run the combined ingest (idempotent — dedups on rule_id + new_text).
    try:
        rule_change_id = ingest_rule_change(
            jurisdiction_name=body.jurisdiction.strip(),
            code_section=body.code_section.strip(),
            old_text=(body.old_text.strip() if body.old_text else None),
            new_text=new_text,
            effective_date=body.effective_date,
            source_url=body.source_url.strip(),
            change_type=change_type,
            embedding=embedding,
        )
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "database_error", "detail": f"DB error during ingest: {exc.__class__.__name__}"},
        )

    # 4. Look up the rule_id + jurisdiction_id for the response (already
    #    persisted above, so this lookup is cheap and consistent).
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                SELECT rc.rule_id, r.jurisdiction_id, rc.change_type
                  FROM rule_changes rc
                  JOIN rules r ON r.id = rc.rule_id
                 WHERE rc.id = %s
                 LIMIT 1;
                """,
                (rule_change_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=500,
                    detail={"error": "internal_error", "detail": "Ingested row not found immediately after insert"},
                )
            rule_id = str(row["rule_id"])
            jurisdiction_id = str(row["jurisdiction_id"])
            stored_ct = row["change_type"]
            resolved_ct: Literal["new", "amended", "repealed", "clarification"] = (
                stored_ct if stored_ct in ("new", "amended", "repealed", "clarification") else "amended"
            )
    except HTTPException:
        raise
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "database_error", "detail": f"Post-ingest lookup failed: {exc.__class__.__name__}"},
        )

    return IngestRuleChangeResponse(
        rule_change_id=rule_change_id,
        rule_id=rule_id,
        jurisdiction_id=jurisdiction_id,
        change_type=resolved_ct,
    )


# ---------------------------------------------------------------------------
# 6. GET /projects — chat UI autocomplete for /permit-check
# ---------------------------------------------------------------------------
class ProjectOut(BaseModel):
    id: str
    name: str
    jurisdiction_id: str
    jurisdiction_name: Optional[str] = None
    occupancy_type: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


@app.get("/projects", response_model=list[ProjectOut])
def list_projects_endpoint(
    search: Optional[str] = Query(
        default=None,
        description="Case-insensitive substring match against project name.",
    ),
    jurisdiction_id: Optional[str] = Query(
        default=None,
        description="Filter by jurisdiction UUID.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ProjectOut]:
    """List projects — used by the chat UI to autocomplete /permit-check args.

    Supports an optional `search` query param for substring-matching project
    names (ILIKE — case-insensitive).
    """
    try:
        rows = list_projects(
            search=search,
            jurisdiction_id=jurisdiction_id,
            limit=limit,
        )
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "database_error", "detail": f"DB error: {exc.__class__.__name__}"},
        )
    return [
        ProjectOut(
            id=str(r["id"]),
            name=r["name"],
            jurisdiction_id=str(r["jurisdiction_id"]),
            jurisdiction_name=r.get("jurisdiction_name"),
            occupancy_type=r.get("occupancy_type"),
            metadata=r.get("metadata"),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 7. GET /rule-changes/{rule_change_id} — chat UI /diff command
# ---------------------------------------------------------------------------
class RuleChangeDetailOut(BaseModel):
    rule_change_id: str
    rule_id: Optional[str] = None
    code_section: Optional[str] = None
    source_url: Optional[str] = None
    change_type: Literal["new", "amended", "repealed", "clarification"]
    old_text: Optional[str] = None
    new_text: str
    effective_date: Optional[date] = None
    detected_at: Optional[datetime] = None
    jurisdiction_id: Optional[str] = None
    jurisdiction_name: Optional[str] = None


@app.get(
    "/rule-changes/{rule_change_id}",
    response_model=RuleChangeDetailOut,
    responses={404: {"model": ErrorEnvelope}},
)
def get_rule_change_endpoint(rule_change_id: str) -> RuleChangeDetailOut:
    """Return a single rule_change with all detail needed for /diff rendering.

    404 if the id is not found.
    """
    try:
        row = get_rule_change_detail(rule_change_id)
    except psycopg.Error as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "database_error", "detail": f"DB error: {exc.__class__.__name__}"},
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "detail": f"rule_change_id={rule_change_id!r} not found",
            },
        )
    ct_raw = row.get("change_type") or "amended"
    change_type: Literal["new", "amended", "repealed", "clarification"] = (
        ct_raw if ct_raw in ("new", "amended", "repealed", "clarification") else "amended"
    )
    return RuleChangeDetailOut(
        rule_change_id=str(row["rule_change_id"]),
        rule_id=str(row["rule_id"]) if row.get("rule_id") else None,
        code_section=row.get("code_section"),
        source_url=row.get("source_url"),
        change_type=change_type,
        old_text=row.get("old_text"),
        new_text=row.get("new_text") or "",
        effective_date=row.get("effective_date"),  # type: ignore[arg-type]
        detected_at=row.get("detected_at"),  # type: ignore[arg-type]
        jurisdiction_id=str(row["jurisdiction_id"]) if row.get("jurisdiction_id") else None,
        jurisdiction_name=row.get("jurisdiction_name"),
    )
