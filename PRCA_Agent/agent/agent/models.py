"""Pydantic domain models + LangGraph GraphState TypedDict.

All structured data flowing through the compliance-check agent lives here.
Keeping schemas in one module lets nodes, alert builders, API routes, and
test harnesses share identical shapes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field, field_validator


DISCLAIMER_SENTENCE = (
    "Flagged for review — not a legal compliance determination."
)


# ---------------------------------------------------------------------------
# Prompt templates used by the agent graph LLM nodes.
# ---------------------------------------------------------------------------
CLASSIFY_PROMPT = """\
You are a regulatory-change analyst for a construction compliance agent.

You are given ONE regulatory rule change to classify — nothing else.
Do NOT use any outside knowledge. Base your answer ONLY on the texts below.

Jurisdiction: {jurisdiction}
Code section: {code_section}
Effective date (may be 'unknown'): {effective_date}

OLD rule text:
---
{old_text}
---

NEW rule text:
---
{new_text}
---

Tasks:
1. Classify the change_type into exactly one of:
   "new"           — rule did not exist before
   "amended"       — existing rule text changed substantively
   "repealed"      — existing rule is being removed / nullified
   "clarification" — wording changed but substantive meaning did not
2. List WHICH of these design attributes are plausibly affected by this
   change. Use the EXACT field names:
     stories, occupancy_type, structural_system,
     fire_resistance_rating_hours, setback_ft, max_occupant_load
   Use [] if you are unsure or the change doesn't cleanly map to any of them.
3. Write a one-sentence rationale grounded ONLY in the old/new text above.
   Do NOT quote long passages, and do NOT invent facts not present in the input.
4. You MUST NOT invent rule content. If the text is ambiguous, say so in the
   rationale and return affected_attributes = [].
"""


IMPACT_LLM_PROMPT = """\
You are a construction regulatory compliance analyst.

You are analysing ONE regulatory rule change against ONE project. Your output
must be a single ComplianceFinding object.

Hard rules you MUST obey:
- Your cited_rule_text MUST be a VERBATIM substring copied from the NEW or OLD
  rule text below. Copy it exactly; do not paraphrase.
- Your confidence MUST be <= {cap} because this is an LLM-only, non-numeric
  analysis.  Deterministic numeric checks always produce higher confidence.
- Your explanation MUST end with the exact sentence:
    "{disclaimer}"
  Do not alter the disclaimer text.
- Do NOT use outside knowledge. Your analysis must follow only from the texts
  provided below.
- If you cannot find a plausible conflict or you are unsure, set status =
  "needs_review" and explain why, rather than inventing a problem.

Jurisdiction: {jurisdiction}
Code section: {code_section}
Source URL (paste unchanged into source_url): {source_url}

OLD rule text:
---
{old_text}
---

NEW rule text:
---
{new_text}
---

PROJECT TO ANALYSE:
  project_id (paste unchanged into project_id): {project_id}
  name: {project_name}
  occupancy_type: {occupancy_type}
  metadata:
    {project_metadata}
  structured design attributes:
    {design_attrs}

Now produce the ComplianceFinding object:
- matched_attribute: pick ONE of the DesignAttributes field names (or null if no
  clean match),
- status = "flagged" only if the new rule clearly conflicts with the project's
  design or metadata; else "compliant" if clearly compliant, else
  "needs_review".
- confidence: use a value between 0.4–{cap} (NEVER higher than {cap}).
- cited_rule_text: a SHORT verbatim quote from the NEW rule supporting your
  finding. Never longer than ~240 chars.
- explanation: 2–4 sentences describing the conflict (or lack thereof), then
  the required disclaimer sentence.
"""


# ---------------------------------------------------------------------------
# RuleChange — input to the compliance check graph
# ---------------------------------------------------------------------------
class RuleChange(BaseModel):
    """A detected change to a regulatory rule (or a proposed change to evaluate)."""

    jurisdiction: str = Field(
        ..., description="Jurisdiction name, e.g. 'Boston' or 'England'."
    )
    code_section: str = Field(
        ..., description="Regulation identifier, e.g. 'IBC 711.3' or 'CODE-ACCORD:5_UK_DocB'."
    )
    old_text: Optional[str] = Field(
        default=None, description="Previous rule text (None for brand-new rules)."
    )
    new_text: str = Field(
        ..., description="Current / amended rule text. NEVER invent content here."
    )
    effective_date: Optional[date] = Field(
        default=None, description="When the change takes effect, if known."
    )
    source_url: str = Field(
        ..., description="Link back to the authoritative source of this rule change."
    )
    change_type: Optional[
        Literal["new", "amended", "repealed", "clarification"]
    ] = Field(
        default=None,
        description="Filled in by classify_node; None until classification runs.",
    )


# ---------------------------------------------------------------------------
# DesignAttributes — structured CAD-agent output for a project
# ---------------------------------------------------------------------------
class DesignAttributes(BaseModel):
    """Structured design properties a CAD/BIM agent would export.

    Every field is optional — real partial designs frequently omit values
    until later engineering phases. Missing fields simply skip their
    deterministic numeric comparisons in the impact node.
    """

    stories: Optional[int] = Field(default=None, ge=1, description="Stories above grade.")
    occupancy_type: Optional[str] = Field(
        default=None, description="IBC-style code, e.g. 'R-2', 'B', 'M', or free-form label."
    )
    structural_system: Optional[str] = Field(
        default=None, description="e.g. 'light wood frame', 'steel frame', 'CMU'."
    )
    fire_resistance_rating_hours: Optional[float] = Field(
        default=None, ge=0, description="Minimum fire-resistance rating in hours."
    )
    setback_ft: Optional[float] = Field(
        default=None, ge=0, description="Front/typical setback in feet."
    )
    max_occupant_load: Optional[int] = Field(
        default=None, ge=0, description="Maximum occupant load (persons)."
    )

    @classmethod
    def from_snapshot_payload(cls, payload: dict[str, Any]) -> "DesignAttributes":
        """Build from a design_snapshots.payload jsonb dict (CAD agents use slightly different key names."""
        return cls(
            stories=payload.get("stories_above_grade") or payload.get("stories"),
            occupancy_type=payload.get("occupancy_type_ibc") or payload.get("occupancy_type"),
            structural_system=payload.get("structural_system"),
            fire_resistance_rating_hours=payload.get("fire_resistance_rating_hours"),
            setback_ft=payload.get("setback_front_ft") or payload.get("setback_ft"),
            max_occupant_load=payload.get("max_occupant_load"),
        )


# ---------------------------------------------------------------------------
# ComplianceFinding — output of the impact/alert nodes
# ---------------------------------------------------------------------------
FindingStatus = Literal["compliant", "flagged", "needs_review"]


class ComplianceFinding(BaseModel):
    """One per (project, rule_change) pair — always carries the disclaimer."""

    project_id: str = Field(..., description="UUID of the affected project.")
    rule_change_id: Optional[str] = Field(
        default=None, description="UUID of the rule_changes row; None for ad-hoc checks."
    )
    status: FindingStatus = Field(
        ..., description="'compliant' | 'flagged' | 'needs_review'."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="0 = guess, 1 = deterministic certainty."
    )
    explanation: str = Field(
        ..., description="Human-readable reasoning. Ends with the disclaimer sentence."
    )
    cited_rule_text: str = Field(
        ..., description="Verbatim quote from the rule (or old->new diff) supporting the finding."
    )
    source_url: str = Field(
        ..., description="Authoritative source URL for the rule (from RuleChange)."
    )
    matched_attribute: Optional[str] = Field(
        default=None,
        description="Which DesignAttributes field triggered the finding; None if LLM-only.",
    )

    @field_validator("explanation")
    @classmethod
    def _ends_with_disclaimer(cls, v: str) -> str:
        if not v.strip().endswith(DISCLAIMER_SENTENCE):
            if not v.strip().endswith("."):
                v = v.strip() + "."
            v = v.strip() + " " + DISCLAIMER_SENTENCE
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "project_id": "uuid-...",
                "rule_change_id": "uuid-...",
                "status": "flagged",
                "confidence": 0.92,
                "explanation": (
                    "Project has fire_resistance_rating_hours=1 but the rule now "
                    "requires >=2. Flagged for review — not a legal compliance determination."
                ),
                "cited_rule_text": "...",
                "source_url": "https://example.com/rule",
                "matched_attribute": "fire_resistance_rating_hours",
            }
        }
    }


# ---------------------------------------------------------------------------
# LLM structured-output helper models — for classify_node
# ---------------------------------------------------------------------------
class ClassificationOutput(BaseModel):
    """Structured output of the classify LLM call."""

    change_type: Literal["new", "amended", "repealed", "clarification"]
    affected_attributes: list[str] = Field(
        default_factory=list,
        description=(
            "Subset of DesignAttributes fields plausibly affected by this change: "
            "stories, occupancy_type, structural_system, fire_resistance_rating_hours, "
            "setback_ft, max_occupant_load. Use [] if unsure."
        ),
    )
    rationale: str = Field(
        ..., description="One-sentence rationale grounded in old/new text."
    )


# ---------------------------------------------------------------------------
# LangGraph state — dict-based (TypedDict), as required by StateGraph
# ---------------------------------------------------------------------------
class ProjectWithContext(BaseModel):
    """A project joined with its latest design snapshot payload."""

    project_id: str
    name: str
    jurisdiction_id: str
    occupancy_type: Optional[str]
    metadata: dict[str, Any]
    design: DesignAttributes


class GraphState(TypedDict, total=False):
    """Shared state flowing through the compliance-check StateGraph.

    All keys are optional (total=False) so partial state is valid. Nodes read
    only what they need and write back the keys they produce.
    """

    rule_change: RuleChange
    jurisdiction_id: Optional[str]
    retrieved_rules: list[dict[str, Any]]
    matched_projects: list[ProjectWithContext]
    classification: Optional[ClassificationOutput]
    raw_findings: list[dict[str, Any]]
    findings: list[ComplianceFinding]
    errors: list[str]
    notify_project_ids: list[str]
    needs_review_project_ids: list[str]
