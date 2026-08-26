# 04 — Rule Engine

> Prerequisite: `01`–`03` complete.
> Paste the prompt below to your AI coding assistant.

---

## Prompt

Build the **Rule Engine** — the brain of Macadamia Impact and the reason it can be trusted. Every judgment the system makes comes from an explicit, hand-authored, versioned engineering rule. The LLM (added in the next module) will only format what this engine decides. Zero LLM calls in this module.

### What to build

In `apps/api/app/pipeline/`:

**1. `rules/engine.py`** — evaluation core:

- `evaluate(change_set: ChangeSet, blast: BlastRadius, db_records: dict[UUID, Record]) -> list[RuleFinding]`
- Loads all registered rules, runs each against the change set + blast radius, collects findings.
- Each finding persists to `rule_findings` with: `rule_id`, `severity`, `category`, `trigger_change`, `affected_record_ids`, `graph_paths` (copied from the BlastRadius paths — provenance), `explanation_data` (the facts: thresholds, actual values, percentages), `suggested_action` (from the rule's template — plain string interpolation, NOT generated text).

**2. Rule definition contract** — `rules/base.py`:

```python
class Rule(ABC):
    rule_id: str            # e.g. "FLOW-001" — stable forever, never reused
    version: int            # bump on any logic change
    category: Category
    title: str              # "Motor sizing review on large flow increase"
    suggested_action_template: str  # "Create engineering review for {entity_tag}: {field} changed {old}→{new} {unit} ({pct:+.1f}%)"

    @abstractmethod
    def check(self, change_set, blast, records) -> list[RuleFinding]: ...
```

Rules are registered in `rules/registry.py` — a plain list. No decorators-magic, no auto-discovery: explicit is the point. An auditor must be able to read one file and see every judgment the system can make.

**3. The initial rule set** — `rules/engineering.py`, `rules/procurement.py`, etc. Implement these six:

| ID | Category | Logic |
|---|---|---|
| `FLOW-001` | engineering | Pump `flow` change with `abs(change_pct) > 20` → severity `critical`; reach any `motor` entity connected via `drives`/`sizes` edge → motor sizing review finding |
| `STALE-001` | procurement | Changed field's new value ≠ value stored in a linked `purchase_order` record's payload (match by entity tag + field), and PO `status` = `current` → `critical`, "flag PO for review" |
| `STALE-002` | documentation | Same staleness check against `datasheet` and `pid` records → `warning`, "mark document stale" |
| `COST-001` | cost | Any `critical` finding on an entity with a linked `cost_estimate` → `warning` on the estimate, "re-validate cost line" |
| `SCHED-001` | schedule | `STALE-001` fired on a PO → any linked `schedule_task` gets `warning`, "check schedule dependency on invalidated PO" |
| `SAFE-001` | safety | Pump flow **increase** > 15% with a linked `safety_checklist` (edge type `certifies`) → `critical`, "relief/overpressure scenario re-validation required" |

Note `COST-001` and `SCHED-001` are **second-order rules** — they fire off other findings, not raw changes. Implement a simple two-pass approach: pass 1 = change-triggered rules, pass 2 = finding-triggered rules. Document that pass 2 rules can never trigger pass 1 rules (no loops by construction).

**4. Severity is owned by rules, never negotiated.** Each rule hard-codes its severity with a comment citing the engineering rationale (e.g. `# >20% flow: typical motor service-factor margin exceeded`).

**5. Rule versioning table** — add `rule_registry` DB table: `rule_id`, `version`, `title`, `category`, `checksum` (sha256 of the rule's source), `registered_at`. On app startup, sync the code registry into this table; if a rule's checksum changed without a version bump, **refuse to start** with a clear error. This is the audit trail that lets us answer "why did the system say this on March 3rd?" six months later.

**6. Tests** (`tests/pipeline/test_rules.py`):

- Seed scenario: ChangeSet `{P-204.flow: 120→150, +25%}` must fire exactly: `FLOW-001` (critical, affects M-204), `STALE-001` (critical, PO-2044), `STALE-002` (warning, datasheet + P&ID), `COST-001` (warning, CE-09), `SCHED-001` (warning, SCH-31), `SAFE-001` (critical, SAFE-07). Assert the exact set of rule IDs, severities, and affected records.
- Threshold boundary: 20.0% exactly → `FLOW-001` does NOT fire (rule is `> 20`). 20.1% → fires.
- Stale check with PO already `under_review` → `STALE-001` does not fire (already being handled).
- Flow **decrease** of 20%+ → `SAFE-001` does NOT fire (increase-only rule); `FLOW-001` still does.
- Checksum/version guard test: tamper with a rule without version bump → startup check raises.
- Every finding's `graph_paths` is non-empty and starts at the changed entity.

### Non-negotiables for this module

- No rule may call an LLM, read from the network, or produce random output. Rules are pure functions of (change, graph, records).
- `explanation_data` must contain every number used in the decision (threshold AND actual value) so the report layer can show its work.
- New rules are added by writing a new `Rule` subclass + registry entry + test. Nothing else. If the AI wants a "generic configurable rule DSL" — decline; explicit subclasses are the audit surface.

### Acceptance criteria

- [ ] Seed scenario fires exactly the 6 expected findings with correct severities/categories
- [ ] Boundary tests pass at exactly 20.0% / 20.1%
- [ ] `rule_registry` table syncs on startup; checksum tampering blocks startup
- [ ] Full test suite green

Show me the six `RuleFinding` JSON objects for the seed scenario when done.
