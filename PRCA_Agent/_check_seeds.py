"""Import-only sanity check for seed_code_accord.py and seed_projects.py.

Does not hit the DB or network — only verifies:
- Scripts compile and can import their dependencies.
- Helper functions produce the expected output shapes.
- Jurisdiction/CSV parsing logic works on the real local files.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
AGENT_PKG_DIR = REPO_ROOT / "agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_PKG_DIR))

# ---------------------------------------------------------------
# CODE-ACCORD
# ---------------------------------------------------------------
import seed_code_accord as sca  # noqa: E402

# Check jurisdiction inference
assert sca._jurisdiction_for_doc_id("69_Finnish_FireSafety") == "Finland"
assert sca._jurisdiction_for_doc_id("105_UK_DocM_V2_AccessAndUseOfBuildings") == "England"
assert sca._jurisdiction_for_doc_id("Doc_A") == "England"
assert sca._jurisdiction_for_doc_id("Approved_Document_B") == "England"
print("[seed_code_accord] doc_id jurisdiction mapping OK")

# Check parsing real entities CSV
rows = sca._load_entities()
assert isinstance(rows, list) and len(rows) > 0
print(f"[seed_code_accord] _load_entities OK, {len(rows)} rows")

# Jurisdiction counts make sense (Finland > 0, England > 0)
from collections import Counter  # noqa: E402
counts = Counter(r["jurisdiction"] for r in rows)
print(f"[seed_code_accord] jurisdiction counts: {dict(counts)}")
assert counts["Finland"] > 0
assert counts["England"] > 0

# Vector literal rendering
from agent.db import _to_pgvector_literal as vec_lit  # noqa: E402
lit = vec_lit([0.1, 0.2, 0.3, -0.4])
assert lit.startswith("[") and lit.endswith("]") and "0.1" in lit
print(f"[seed_code_accord] pgvector literal: {lit}")

# ---------------------------------------------------------------
# BOSTON / PROJECTS
# ---------------------------------------------------------------
import seed_projects as sp  # noqa: E402

# Occupancy canonicalization
assert "Multi-Family" in sp._canonicalize_occupancy("Multi-Family")
assert "Mixed-Use" in sp._canonicalize_occupancy("Mixed")
assert "Office" in sp._canonicalize_occupancy("Office")
assert "1-2 Family" in sp._canonicalize_occupancy("1-2FAM")
print("[seed_projects] occupancy canonicalization OK")

# Project names list length matches spec
assert len(sp.PROJECT_NAMES) == 10, f"expected 10 project names, got {len(sp.PROJECT_NAMES)}"
assert len(sp.JURISDICTION_TREE) == 3  # US, MA, Boston
assert sp.JURISDICTION_TREE[-1][0] == "Boston"
print("[seed_projects] jurisdiction tree OK")

# Design snapshot construction (no DB, no permit needed)
fake_permit = {"permittypedescr": "New Construction", "occupancytype": "Mixed"}
snap = sp._design_snapshot_for(idx=0,
                                occupancy_type="Mixed-Use (Commercial + Residential)",
                                sq_feet=12000,
                                valuation=3_500_000,
                                permit=fake_permit)
required_keys = {"stories_above_grade", "occupancy_type_ibc",
                 "gross_square_feet", "structural_system",
                 "fire_resistance_rating_hours", "setback_front_ft",
                 "max_occupant_load"}
assert required_keys.issubset(snap.keys()), f"missing keys: {required_keys - set(snap.keys())}"
assert isinstance(snap["stories_above_grade"], int) and snap["stories_above_grade"] > 0
assert isinstance(snap["gross_square_feet"], int) and snap["gross_square_feet"] > 0
assert 0 <= snap["setback_front_ft"] <= 200
print(f"[seed_projects] sample design_snapshot payload keys: {sorted(snap.keys())}")

# Permit selection logic (no HTTP) — mock out some sample permits
mock_permits = []
permit_nums = [
    ("A1", "Multi-Family"), ("A2", "Multi-Family"), ("A3", "Multi-Family"),
    ("B1", "1-2FAM"), ("B2", "1unit"),
    ("C1", "Office"), ("C2", "Retail"),
    ("D1", "Mixed"), ("D2", "Mixed"),
    ("E1", "Commercial"), ("E2", "Industrial"),
]
for pn, ot in permit_nums:
    mock_permits.append({
        "_id": len(mock_permits), "permitnumber": pn,
        "occupancytype": ot,
        "address": f"100 {pn} St", "city": "Boston", "state": "MA", "zip": "02100",
        "declared_valuation": "$1,000,000.00", "sq_feet": "2500",
        "issued_date": "2023-01-01", "permittypedescr": "Long Form",
        "worktype": "NEWC", "applicant": "Acme", "status": "Issued",
    })
seeds = sp._pick_project_seeds(mock_permits)
assert len(seeds) == 10, f"expected 10 seeds, got {len(seeds)}"
# Confirm the spec-required occupancies are covered
occupancies = set()
multi = 0
comm = 0
mixed = 0
for s in seeds:
    occ = sp._canonicalize_occupancy(s.get("occupancytype"))
    occupancies.add(occ)
    if "Multi" in occ:
        multi += 1
    if "Commercial:" in occ or "Retail:" in occ or "Office" in occ:
        comm += 1
    if "Mixed-Use" in occ:
        mixed += 1
print(f"[seed_projects] seed counts: multi={multi}  comm={comm}  mixed={mixed}")
assert multi >= 2, "need at least 2 multi-family"
assert comm >= 1, "need at least 1 commercial"
assert mixed >= 1, "need at least 1 mixed-use"

# Build synthetic projects from mocked permits
projects = sp._projects_from_seeds(seeds, sp.PROJECT_NAMES)
assert len(projects) == 10
for p in projects:
    assert set(p.keys()) == {"name", "occupancy_type", "metadata", "design_snapshot"}
    assert p["metadata"].get("permit_id") is not None
    assert p["metadata"].get("declared_valuation_usd") == 1_000_000.0
print("[seed_projects] projects_from_seeds OK, 10 projects generated")

print()
print("=" * 60)
print("ALL IMPORT-LEVEL SANITY CHECKS PASSED")
print("=" * 60)
