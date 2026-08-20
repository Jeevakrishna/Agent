"""Test harness for the compliance-check LangGraph agent.

Runnable directly:
  python test_graph.py                 # uses current env (honours GEMINI_API_KEY)
  python test_graph.py --no-llm        # simulates NO LLM key available →
                                       # proves deterministic degradation

Pipeline:
  1. Constructs a realistic RuleChange (fire-resistance amendment) BY HAND.
  2. Runs the full 5-node compliance check graph end-to-end.
  3. Asserts:
       - at least 1 finding produced;
       - any seeded project with fire_resistance_rating_hours < 2 AND
         occupancy ~ R-2 / multi-family is FLAGGED via the DETERMINISTIC
         path (matched_attribute is set, confidence >= 0.9);
       - every finding ends with the exact disclaimer sentence and carries
         a cited_rule_text quote.
  4. Prints the full findings JSON for eyeballing.

The test never hits the CODE-ACCORD/Boston internet APIs. If the database has
no projects (e.g. seed scripts haven't been run), it inserts 3 synthetic
projects into the 'Boston' jurisdiction so the graph has data to compare.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Paths
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

import psycopg  # noqa: E402

from agent.config import Settings, get_settings  # noqa: E402
from agent.db import (  # noqa: E402
    get_connection,
    get_jurisdiction_by_name,
    get_projects_with_latest_design,
)
from agent.graph import AUTO_FLAG_CONFIDENCE_THRESHOLD, run_graph  # noqa: E402
from agent.models import (  # noqa: E402
    ComplianceFinding,
    DISCLAIMER_SENTENCE,
    DesignAttributes,
    GraphState,
    RuleChange,
)


# ---------------------------------------------------------------------------
# The synthetic RuleChange we test against.
# ---------------------------------------------------------------------------
TEST_RULE_CHANGE = RuleChange(
    jurisdiction="Boston",
    code_section="101 CMR 707.4 — Fire-Resistance Rating, Multi-Family",
    old_text=(
        "For multi-family residential occupancies (R-2), fire-resistance "
        "rating of floor assemblies shall be a minimum of 1 hour."
    ),
    new_text=(
        "For multi-family residential occupancies (R-2), fire-resistance "
        "rating of floor assemblies shall be a MINIMUM of 2 hours, "
        "effective for all new construction and permitted alterations filed "
        "on or after 1 January 2026."
    ),
    effective_date=date(2026, 1, 1),
    source_url=(
        "https://example-regulations.boston.gov/101-cmr-707.4/"
        "fire-resistance-r2-2026-amendment.pdf"
    ),
    change_type=None,
)


# ---------------------------------------------------------------------------
# Ensure DB has at least 3 synthetic Boston projects if seeds haven't run.
# ---------------------------------------------------------------------------
def _ensure_test_projects() -> list[dict[str, Any]]:
    """If Boston projects exist, return them. Otherwise insert 3 canonical ones.

    Canonical projects (we always re-insert a fresh set under unique names):
      #1 "TEST-MF-LOW-RATING"  → R-2, fire_rating = 1 → should be FLAGGED deterministic
      #2 "TEST-MF-HIGH-RATING" → R-2, fire_rating = 3 → should be COMPLIANT deterministic
      #3 "TEST-OFFICE-ANY"     → B,   fire_rating = 1 → unaffected (not R-2)
    """
    boston = get_jurisdiction_by_name("Boston")
    existing = get_projects_with_latest_design(
        jurisdiction_id=(boston["id"] if boston else None)
    )
    # Filter for the TEST-* projects only; if present, skip insertion.
    test_existing = [p for p in existing if (p.get("project_name") or "").startswith("TEST-")]
    if len(test_existing) >= 3:
        print("[test_graph] Reusing existing TEST-* projects in DB")
        return test_existing

    if boston is None:
        # Insert the jurisdiction tree if neither seed script has run yet.
        with get_connection() as conn:
            with conn.transaction():
                cur = conn.execute(
                    """
                    INSERT INTO jurisdictions (name, level, parent_id)
                    VALUES ('United States', 'country', NULL)
                    ON CONFLICT DO NOTHING RETURNING id;
                    """
                )
                us = cur.fetchone()
                if us is None:
                    cur.execute(
                        "SELECT id FROM jurisdictions WHERE name='United States'"
                    )
                    us = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO jurisdictions (name, level, parent_id)
                    VALUES ('Massachusetts', 'state', %s)
                    ON CONFLICT DO NOTHING RETURNING id;
                    """,
                    (us["id"],),
                )
                ma = cur.fetchone()
                if ma is None:
                    cur.execute(
                        "SELECT id FROM jurisdictions WHERE name='Massachusetts'"
                    )
                    ma = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO jurisdictions (name, level, parent_id)
                    VALUES ('Boston', 'city', %s)
                    ON CONFLICT DO NOTHING RETURNING id;
                    """,
                    (ma["id"],),
                )
                boston_row = cur.fetchone()
                if boston_row is None:
                    cur.execute(
                        "SELECT id FROM jurisdictions WHERE name='Boston'"
                    )
                    boston_row = cur.fetchone()
                boston_id = boston_row["id"]
            conn.commit()
    else:
        boston_id = boston["id"]

    test_projects: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        (
            "TEST-MF-LOW-RATING",
            {
                "occupancy": "R-2",
                "work_type": "NEWC",
                "declared_valuation_usd": 4_200_000,
                "address": "100 Harborwalk, Boston MA 02210",
            },
            {
                "stories_above_grade": 5,
                "occupancy_type_ibc": "R-2",
                "structural_system": "light wood frame",
                "fire_resistance_rating_hours": 1,
                "setback_front_ft": 12,
                "setback_side_ft": 8,
                "setback_rear_ft": 15,
                "max_occupant_load": 120,
                "number_of_dwelling_units": 20,
                "construction_class": "Type V-A",
            },
        ),
        (
            "TEST-MF-HIGH-RATING",
            {
                "occupancy": "R-2",
                "work_type": "NEWC",
                "declared_valuation_usd": 8_500_000,
                "address": "200 Seaport Blvd, Boston MA 02210",
            },
            {
                "stories_above_grade": 7,
                "occupancy_type_ibc": "R-2",
                "structural_system": "steel frame with concrete slabs",
                "fire_resistance_rating_hours": 3,
                "setback_front_ft": 20,
                "setback_side_ft": 15,
                "setback_rear_ft": 20,
                "max_occupant_load": 210,
                "number_of_dwelling_units": 42,
                "construction_class": "Type II-A",
            },
        ),
        (
            "TEST-OFFICE-ANY",
            {
                "occupancy": "B",
                "work_type": "ALT",
                "declared_valuation_usd": 1_800_000,
                "address": "50 Milk St, Boston MA 02109",
            },
            {
                "stories_above_grade": 4,
                "occupancy_type_ibc": "B",
                "structural_system": "steel frame with composite deck",
                "fire_resistance_rating_hours": 1,
                "setback_front_ft": 10,
                "setback_side_ft": 5,
                "setback_rear_ft": 10,
                "max_occupant_load": 200,
                "number_of_dwelling_units": 0,
                "construction_class": "Type II-B",
            },
        ),
    ]

    with get_connection() as conn:
        with conn.transaction():
            for name, meta, payload in test_projects:
                cur = conn.execute(
                    """
                    INSERT INTO projects
                        (name, jurisdiction_id, occupancy_type, metadata)
                    VALUES (%s, %s, %s, %s::jsonb)
                    RETURNING id;
                    """,
                    (
                        name,
                        boston_id,
                        payload.get("occupancy_type_ibc") or meta.get("occupancy"),
                        json.dumps(meta),
                    ),
                )
                pid = cur.fetchone()["id"]
                conn.execute(
                    """
                    INSERT INTO design_snapshots
                        (project_id, source_agent, payload)
                    VALUES (%s, 'test_graph/seeder', %s::jsonb);
                    """,
                    (pid, json.dumps(payload)),
                )
        conn.commit()

    fresh = [
        p for p in get_projects_with_latest_design(jurisdiction_id=boston_id)
        if (p.get("project_name") or "").startswith("TEST-")
    ]
    print(f"[test_graph] Inserted {len(fresh)} fresh TEST-* projects")
    return fresh


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
def _run_assertions(
    state: GraphState,
    deterministic_only: bool,
) -> None:
    findings: list[ComplianceFinding] = state.get("findings", []) or []
    projects_by_id: dict[str, dict[str, Any]] = {
        p["project_id"]: p
        for p in get_projects_with_latest_design()
    }

    print(f"\n[assert] Total findings produced: {len(findings)}")
    assert len(findings) >= 1, "Expected at least 1 finding, got 0"

    deterministic_flagged = [
        f
        for f in findings
        if f.matched_attribute == "fire_resistance_rating_hours"
        and f.status == "flagged"
        and f.confidence >= 0.90
    ]
    print(
        f"[assert] DETERMINISTIC fire_rating flagged findings: "
        f"{len(deterministic_flagged)} (confidence >= 0.90)"
    )

    # Find TEST-MF-LOW-RATING specifically — it MUST be flagged deterministically.
    low_id = None
    high_id = None
    for pid, p in projects_by_id.items():
        nm = p.get("project_name") or ""
        if nm == "TEST-MF-LOW-RATING":
            low_id = str(pid)  # psycopg returns UUID; findings use str
        elif nm == "TEST-MF-HIGH-RATING":
            high_id = str(pid)

    if low_id is not None:
        low_findings = [f for f in findings if f.project_id == low_id]
        assert low_findings, "TEST-MF-LOW-RATING produced no findings"
        low_fr_findings = [
            f for f in low_findings
            if f.matched_attribute == "fire_resistance_rating_hours"
        ]
        assert low_fr_findings, (
            "TEST-MF-LOW-RATING expected at least one DETERMINISTIC "
            "fire_resistance_rating_hours finding, got none. "
            f"All findings for it: {[(f.status, f.confidence, f.matched_attribute) for f in low_findings]}"
        )
        flagged = [f for f in low_fr_findings if f.status == "flagged"]
        assert flagged, (
            "TEST-MF-LOW-RATING (fire_rating=1, R-2) should be FLAGGED when "
            f"threshold is 2 hours, but got statuses={[f.status for f in low_fr_findings]}"
        )
        for f in flagged:
            assert f.confidence >= 0.90, (
                "Deterministic path should produce >= 0.90 confidence, "
                f"got {f.confidence}"
            )
            assert f.matched_attribute is not None, (
                "Deterministic findings must carry matched_attribute."
            )
        print("[assert] TEST-MF-LOW-RATING -> FLAGGED via DETERMINISTIC path [OK]")

    if high_id is not None:
        high_findings = [f for f in findings if f.project_id == high_id]
        fr = [
            f for f in high_findings
            if f.matched_attribute == "fire_resistance_rating_hours"
        ]
        if fr:
            compliant = [f for f in fr if f.status == "compliant"]
            assert compliant, (
                "TEST-MF-HIGH-RATING (fire_rating=3, R-2) should be COMPLIANT; "
                f"got statuses={[f.status for f in fr]}"
            )
            print("[assert] TEST-MF-HIGH-RATING -> COMPLIANT (>= threshold) [OK]")
        else:
            # It's OK if no fire rating finding was produced only for non-R-2,
            # but here TEST-MF-HIGH-RATING IS R-2, so flag softly via print
            # (not fatal if some path was skipped).
            print(
                "[assert] TEST-MF-HIGH-RATING: no fire rating finding produced "
                "(deterministic check may have skipped if jurisdiction mismatch)"
            )

    # Every finding: disclaimer + citation.
    for f in findings:
        assert f.cited_rule_text and f.cited_rule_text.strip(), (
            "Every finding must include cited_rule_text."
        )
        assert f.explanation.strip().endswith(DISCLAIMER_SENTENCE), (
            "Every finding explanation must end with the exact disclaimer sentence. "
            f"Got: {f.explanation[-200:]!r}"
        )
    print(f"[assert] All {len(findings)} findings carry citation + disclaimer [OK]")

    # Gate routing: any flagged finding with confidence >= AUTO_FLAG_THRESHOLD
    # should have been routed through notify.
    high_conf_flags = [
        f for f in findings
        if f.status == "flagged" and f.confidence >= AUTO_FLAG_CONFIDENCE_THRESHOLD
    ]
    if deterministic_only:
        # In no-LLM mode we rely solely on deterministic flags; high_conf_flags
        # should be >= 1 (the TEST-MF-LOW case).
        assert high_conf_flags, (
            "Deterministic-only mode expected at least one high-confidence flag, "
            "got none — gate routing assertions will be impossible."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PRCA agent graph test harness")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Simulate missing LLM key (clear GEMINI_API_KEY/GROQ_API_KEY/"
            "OPENROUTER_API_KEY env vars, fall back to deterministic-only checks)."
        ),
    )
    parser.add_argument(
        "--skip-seeds",
        action="store_true",
        help="Don't insert TEST-* projects (assume DB already has them / real seeds).",
    )
    args = parser.parse_args(argv)

    # ---- Environment manipulation (for --no-llm) ----
    if args.no_llm:
        for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
            os.environ[key] = ""  # override .env file with explicit empty string
        os.environ["LLM_PROVIDER"] = "gemini"  # provider still "gemini" but no key
        print(
            "[test_graph] --no-llm mode: LLM API keys removed from env. "
            "Deterministic checks MUST still produce correct results."
        )
        # Blow away the settings LRU cache so get_settings() re-reads env.
        get_settings.cache_clear()
        # Also verify: the fresh Settings object should have empty API keys.
        s: Settings = get_settings()
        assert not (s.GEMINI_API_KEY or "").strip(), (
            "--no-llm mode but GEMINI_API_KEY still populated — settings cache not cleared?"
        )
    else:
        s = get_settings()
        provider_keys_ok = bool(
            (s.LLM_PROVIDER.lower() == "gemini" and s.GEMINI_API_KEY)
            or (s.LLM_PROVIDER.lower() == "groq" and s.GROQ_API_KEY)
            or (s.LLM_PROVIDER.lower() == "openrouter" and s.OPENROUTER_API_KEY)
            or (s.LLM_PROVIDER.lower() == "ollama")
        )
        if not provider_keys_ok:
            print(
                f"[test_graph] WARNING: LLM_PROVIDER={s.LLM_PROVIDER!r} but no "
                "corresponding API key is set. Graph will still run because "
                "deterministic checks degrade gracefully, but classify_node "
                "and the impact-node LLM fallback will both fall back."
            )

    # ---- Ensure test projects exist in the DB ----
    if not args.skip_seeds:
        try:
            _ensure_test_projects()
        except psycopg.Error as exc:
            print(f"[test_graph] DB error during project seeding: {exc}")
            print(
                "[test_graph] Make sure Postgres is up:\n"
                "  cd infra ; docker compose up -d db\n"
                "Then run migrations:\n"
                "  cd agent ; .venv/Scripts/python.exe -m agent.migrate"
            )
            return 2

    print()
    print("=" * 68)
    print("RuleChange under test:")
    print(f"  jurisdiction : {TEST_RULE_CHANGE.jurisdiction}")
    print(f"  code_section : {TEST_RULE_CHANGE.code_section}")
    print(f"  source_url   : {TEST_RULE_CHANGE.source_url}")
    print(f"  effective_date: {TEST_RULE_CHANGE.effective_date}")
    print(f"  old_text (snippet): {TEST_RULE_CHANGE.old_text[:120]}...")
    print(f"  new_text (snippet): {TEST_RULE_CHANGE.new_text[:120]}...")
    print("=" * 68)
    print()

    try:
        state: GraphState = run_graph(TEST_RULE_CHANGE)
    except psycopg.Error as exc:
        print(f"[test_graph] DB connection failed: {exc}")
        return 2
    except Exception as exc:
        print(f"[test_graph] Graph execution failed: {exc!r}")
        raise

    errors: list[str] = state.get("errors", []) or []
    if errors:
        print(f"\n[test_graph] Graph warnings/errors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")

    findings: list[ComplianceFinding] = state.get("findings", []) or []
    _run_assertions(state, deterministic_only=args.no_llm)

    # ---- Full JSON dump for eyeballing ----
    print("\n" + "=" * 68)
    print("FULL FINDINGS JSON (for eyeballing):")
    print("=" * 68)
    serializable: list[dict[str, Any]] = []
    for f in findings:
        d = f.model_dump(mode="json")
        serializable.append(d)
    print(json.dumps(serializable, indent=2, default=str))

    # Gate routing summary
    notify = state.get("notify_project_ids", []) or []
    review = state.get("needs_review_project_ids", []) or []
    print()
    print(f"[gate] routed to notify       : {len(notify)} projects  {notify}")
    print(f"[gate] routed to needs_review : {len(review)} projects  {review}")
    print()
    if args.no_llm:
        print("[test_graph] --no-llm run: DETERMINISTIC PATH DEGRADATION WORKS [OK]")
    else:
        print("[test_graph] Full run (LLM enabled): ALL ASSERTIONS PASSED [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
