"""Seed Boston jurisdictions + synthetic projects + design snapshots.

Data source: Analyze Boston (data.boston.gov) CKAN API — free, no API key.
  1. package_show?id=approved-building-permits  -> find datastore resource_id
  2. datastore_search?resource_id=<ID>&limit=500  -> real permit records
     (fields: permitnumber, worktype, permittypedescr, declared_valuation,
      occupancytype, sq_feet, address, city, state, zip, issued_date, ...)

We use the real permit field shapes to populate metadata jsonb but only
create 10 *synthetic* projects — so it's deterministic, repeatable, and
doesn't grow the test DB unbounded. Each synthetic project mirrors a real
Boston permit's occupancy, work type, address, and declared valuation.

NYC stretch: skipped. The Socrata dataset ID for NYC DOB permits is not
obvious without a portal search, and Boston + CODE-ACCORD is sufficient
per the requirements.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

import httpx
import psycopg

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from agent.config import get_settings  # noqa: E402

REPO_ROOT = _THIS_DIR.parent

CKAN_BASE = "https://data.boston.gov/api/3/action"
PACKAGE_ID = "approved-building-permits"
FETCH_LIMIT = 500
HTTP_TIMEOUT = httpx.Timeout(45.0, connect=15.0)

# ---- Jurisdiction hierarchy for Boston ------------------------------------
JURISDICTION_TREE = [
    # (name, level, parent_name_or_None)
    ("United States", "country", None),
    ("Massachusetts", "state", "United States"),
    ("Boston", "city", "Massachusetts"),
]

# ---- Occupancy mapping ----------------------------------------------------
# Boston permits use occupancytype values like: "Mixed", "1-2FAM",
# "Multi-Family", "Commercial", "Office", "Retail", "Industrial", ...
# Map these to canonical descriptions for our `occupancy_type` column.
OCCUPANCY_LABELS: dict[str, str] = {
    "Mixed": "Mixed-Use (Commercial + Residential)",
    "1-2FAM": "Residential: 1-2 Family",
    "Multi-Family": "Residential: Multi-Family (3+ units)",
    "Commercial": "Commercial: General",
    "Office": "Commercial: Office",
    "Retail": "Commercial: Retail",
    "Restaurant": "Commercial: Restaurant",
    "Industrial": "Industrial: Warehouse/Manufacturing",
    "Garage": "Parking Garage",
    "School": "Educational: School",
    "VacLd": "Vacant Land",
    "3FAM-": "Residential: 3 Family",
    "4-7FAM": "Residential: 4-7 Family",
    "7FAM+": "Residential: 8+ Family",
    "2unit": "Residential: 2 Units",
    "1unit": "Residential: Single Family",
}

CANONICAL_OCCUPANCY = {
    "Residential: Multi-Family": ["Multi-Family", "3FAM-", "4-7FAM", "7FAM+"],
    "Residential: 1-2 Family": ["1-2FAM", "1unit", "2unit"],
    "Commercial: Office": ["Office"],
    "Commercial: Retail": ["Retail"],
    "Mixed-Use (Commercial + Residential)": ["Mixed"],
    "Commercial: General": ["Commercial"],
}


def _canonicalize_occupancy(raw: str | None) -> str:
    if not raw:
        return "Residential: Single Family"
    for canonical, variants in CANONICAL_OCCUPANCY.items():
        if raw in variants:
            return canonical
    label = OCCUPANCY_LABELS.get(raw)
    if label:
        return label
    return f"Other: {raw}"


# ---- HTTP helpers ---------------------------------------------------------
@retry(stop=stop_after_attempt(3), wait=wait_fixed(3))
def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(
            f"CKAN API error: {payload.get('error')} (url={url})"
        )
    return payload["result"]


def _find_resource_id() -> str:
    url = f"{CKAN_BASE}/package_show"
    print(f"[boston] package_show id={PACKAGE_ID}")
    result = _get_json(url, params={"id": PACKAGE_ID})
    print(f"[boston]   package title: {result.get('title')!r}")
    resources = result.get("resources", [])
    resource_id: str | None = None
    for res in resources:
        if res.get("datastore_active"):
            resource_id = res["id"]
            name = res.get("name") or "(unnamed)"
            fmt = res.get("format")
            print(f"[boston]   using resource: {resource_id}  ({name}, {fmt})")
            break
    if not resource_id:
        raise RuntimeError("No datastore_active resource found for approved-building-permits")
    return resource_id


def _fetch_permits(resource_id: str, limit: int = FETCH_LIMIT) -> list[dict[str, Any]]:
    url = f"{CKAN_BASE}/datastore_search"
    params = {"resource_id": resource_id, "limit": limit}
    print(f"[boston] datastore_search resource={resource_id} limit={limit}")
    result = _get_json(url, params=params)
    records = result.get("records", [])
    fields = result.get("fields", [])
    total = result.get("total")
    print(f"[boston]   fetched {len(records)} / {total} records")
    print(f"[boston]   fields: {[f['id'] for f in fields]}")

    # Print a few sample occupancytype values so the user can see the real data shapes
    occupancies = [r.get("occupancytype") for r in records if r.get("occupancytype")]
    from collections import Counter
    top = Counter(occupancies).most_common(10)
    print(f"[boston]   top 10 occupancy types: {top}")

    # Print 1 sample permit in full
    sample = records[0]
    print(f"[boston]   sample permit:")
    for k, v in sample.items():
        if v is not None and v != "" and v != {}:
            print(f"      {k}: {repr(v)[:160]}")
    return records


# ---- Jurisdiction insert --------------------------------------------------
def _ensure_jurisdictions(conn: psycopg.Connection) -> dict[str, str]:
    """Insert US -> MA -> Boston hierarchy; return {name: id}."""
    ids: dict[str, str] = {}
    with conn.cursor() as cur:
        for name, level, parent_name in JURISDICTION_TREE:
            parent_id = ids.get(parent_name) if parent_name else None
            cur.execute(
                "SELECT id FROM jurisdictions WHERE name = %s AND level = %s;",
                (name, level),
            )
            row = cur.fetchone()
            if row is not None:
                ids[name] = row[0]
                continue
            cur.execute(
                """
                INSERT INTO jurisdictions (name, level, parent_id)
                VALUES (%s, %s, %s)
                RETURNING id;
                """,
                (name, level, parent_id),
            )
            ids[name] = cur.fetchone()[0]
    conn.commit()
    print(f"[boston] jurisdictions: {ids}")
    return ids


# ---- Project selection + seeding ------------------------------------------
def _pick_project_seeds(permits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """From the 500 real permits, pick 10 to use as seeds for synthetic projects.

    Requirements from the spec: at least 2 multi-family residential,
    1 commercial, 1 mixed-use.
    """
    # Filter to the categories we want
    multi_fam = [p for p in permits if _canonicalize_occupancy(p.get("occupancytype")).startswith("Residential: Multi")]
    one_two = [p for p in permits if _canonicalize_occupancy(p.get("occupancytype")).startswith("Residential: 1-2")]
    office = [p for p in permits if _canonicalize_occupancy(p.get("occupancytype")) == "Commercial: Office"]
    retail = [p for p in permits if _canonicalize_occupancy(p.get("occupancytype")) == "Commercial: Retail"]
    mixed = [p for p in permits if _canonicalize_occupancy(p.get("occupancytype")).startswith("Mixed-Use")]
    general_comm = [p for p in permits if _canonicalize_occupancy(p.get("occupancytype")) == "Commercial: General"]

    picked: list[dict[str, Any]] = []

    def _take(src: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
        out = []
        for p in src:
            if p.get("permitnumber") not in {x.get("permitnumber") for x in picked}:
                out.append(p)
                if len(out) >= n:
                    break
        return out

    picked.extend(_take(multi_fam, 3))    # 3 multi-family (>= 2 required)
    picked.extend(_take(one_two, 2))      # 2 single/2-family
    picked.extend(_take(office, 1))       # 1 commercial (office)
    picked.extend(_take(retail, 1))       # 1 more commercial (retail)
    picked.extend(_take(mixed, 2))        # 2 mixed-use (>= 1 required)
    picked.extend(_take(general_comm, 1)) # 1 general commercial

    # If any buckets came up short (e.g. no permits with exact matches), pad
    # from the full list to guarantee exactly 10.
    if len(picked) < 10:
        for p in permits:
            if p.get("permitnumber") not in {x.get("permitnumber") for x in picked}:
                picked.append(p)
                if len(picked) >= 10:
                    break
    return picked[:10]


def _projects_from_seeds(
    seeds: list[dict[str, Any]],
    project_names: list[str],
) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for idx, permit in enumerate(seeds):
        occ_raw = permit.get("occupancytype")
        occupancy_type = _canonicalize_occupancy(occ_raw)

        valuation_raw = permit.get("declared_valuation") or "$0"
        try:
            valuation_numeric = float(
                valuation_raw.replace("$", "").replace(",", "").strip()
            )
        except (TypeError, ValueError):
            valuation_numeric = 0.0

        sq_feet = permit.get("sq_feet")
        if isinstance(sq_feet, str):
            try:
                sq_feet = float(sq_feet)
            except ValueError:
                sq_feet = None

        address_parts = [
            s for s in [permit.get("address"), permit.get("city"), permit.get("state"), permit.get("zip")]
            if s
        ]

        metadata: dict[str, Any] = {
            "permit_id": permit.get("permitnumber"),
            "boston_permit__id": permit.get("_id"),
            "address": ", ".join(address_parts) if address_parts else None,
            "street_address": permit.get("address"),
            "city": permit.get("city"),
            "state": permit.get("state"),
            "zip": permit.get("zip"),
            "occupancy_raw": occ_raw,
            "work_type": permit.get("worktype"),
            "permit_type_description": permit.get("permittypedescr"),
            "description": permit.get("description"),
            "declared_valuation": valuation_raw,
            "declared_valuation_usd": valuation_numeric,
            "total_fees": permit.get("total_fees"),
            "sq_feet": sq_feet,
            "issued_date": permit.get("issued_date"),
            "expiration_date": permit.get("expiration_date"),
            "permit_status": permit.get("status"),
            "applicant": permit.get("applicant"),
            "ward": permit.get("ward"),
            "property_id": permit.get("property_id"),
            "parcel_id": permit.get("parcel_id"),
            "latitude": permit.get("y_latitude"),
            "longitude": permit.get("x_longitude"),
        }

        projects.append(
            {
                "name": project_names[idx],
                "occupancy_type": occupancy_type,
                "metadata": metadata,
                "design_snapshot": _design_snapshot_for(
                    idx, occupancy_type, sq_feet, valuation_numeric, permit
                ),
            }
        )
    return projects


def _design_snapshot_for(
    idx: int,
    occupancy_type: str,
    sq_feet: Any,
    valuation: float,
    permit: dict[str, Any],
) -> dict[str, Any]:
    """Return a plausible CAD-agent style design_snapshot payload."""
    sq = int(sq_feet) if isinstance(sq_feet, (int, float)) and sq_feet and sq_feet > 0 else 0

    occ = occupancy_type.lower()

    # Plausible stories by occupancy
    if "multi-family" in occ or "mixed-use" in occ:
        stories = max(3, min(12, 3 + (idx % 5)))
        if sq < 1000:
            sq = stories * 4500
    elif "office" in occ or "commercial: general" in occ:
        stories = max(2, min(20, 5 + (idx % 4)))
        if sq < 1000:
            sq = stories * 12000
    elif "retail" in occ:
        stories = 1 if idx % 2 == 0 else 2
        if sq < 1000:
            sq = stories * 6500
    else:  # single/2-family etc.
        stories = 2 if idx % 3 == 0 else 3
        if sq < 1000:
            sq = stories * 1800

    # Structural system / fire rating by occupancy
    if "multi-family" in occ or "mixed-use" in occ:
        structural_system = (
            "light wood frame" if stories <= 5 else "steel frame with concrete slabs"
        )
        fire_rating_hr = 1 if stories <= 3 else 2
    elif "office" in occ:
        structural_system = "steel frame with composite deck"
        fire_rating_hr = 2 if stories > 6 else 1
    elif "retail" in occ:
        structural_system = "CMU + steel bar joist roof"
        fire_rating_hr = 1
    elif "commercial: general" in occ:
        structural_system = "tilt-up concrete walls, steel roof"
        fire_rating_hr = 2
    else:
        structural_system = "light wood frame"
        fire_rating_hr = 1

    # Rough setback by lot size proxy (valuation)
    if valuation > 2_000_000:
        setback_ft = 20
    elif valuation > 500_000:
        setback_ft = 15
    else:
        setback_ft = 10

    # Max occupant load estimate
    if "office" in occ:
        occupant_load_per_1000_sqft = 10
    elif "retail" in occ:
        occupant_load_per_1000_sqft = 30
    elif "mixed" in occ:
        occupant_load_per_1000_sqft = 24
    elif "multi" in occ:
        occupant_load_per_1000_sqft = 20
    else:
        occupant_load_per_1000_sqft = 10
    max_occupant_load = max(8, int(sq * occupant_load_per_1000_sqft / 1000))

    # IBC occupancy code analog
    if "office" in occ:
        ibc_occ = "B"
    elif "retail" in occ:
        ibc_occ = "M"
    elif "restaurant" in occ or "mixed" in occ:
        ibc_occ = "A-2/M" if "retail" in occ else "A-2/R-2"
    elif "multi" in occ:
        ibc_occ = "R-2"
    elif "1-2 family" in occ:
        ibc_occ = "R-3"
    elif "industrial" in occ:
        ibc_occ = "F-1"
    elif "warehouse" in occ:
        ibc_occ = "S-1"
    else:
        ibc_occ = "B"

    permittype = (permit.get("permittypedescr") or "").lower()
    scope_hint = "interior renovation"
    if "new" in permittype or "erect" in permittype:
        scope_hint = "new construction"
    elif "amend" in permittype:
        scope_hint = "permit amendment"
    elif "addit" in permittype:
        scope_hint = "addition + renovation"

    return {
        "stories_above_grade": stories,
        "stories_below_grade": 1 if (valuation > 1_500_000 and idx % 3 == 0) else 0,
        "occupancy_type_ibc": ibc_occ,
        "occupancy_description": occupancy_type,
        "gross_square_feet": sq,
        "structural_system": structural_system,
        "fire_resistance_rating_hours": fire_rating_hr,
        "setback_front_ft": setback_ft,
        "setback_side_ft": max(5, setback_ft - 5),
        "setback_rear_ft": setback_ft,
        "max_occupant_load": max_occupant_load,
        "number_of_dwelling_units": (
            stories * 4 if "multi" in occ else (
                stories * 2 if "mixed" in occ else (2 if "1-2 family" in occ else 0)
            )
        ),
        "construction_class": (
            "Type V-B" if structural_system == "light wood frame" else
            "Type II-A" if "steel frame" in structural_system else
            "Type I-B"
        ),
        "scope_of_work_hint": scope_hint,
        "cad_agent_version": "seed-pipeline-v1",
    }


PROJECT_NAMES = [
    "Harbor Point Residences — Phase 2",
    "Back Bay Brownstone Renovation",
    "Washington St. Mixed-Use Building",
    "Dorchester Multi-Family Infill",
    "Mission Hill 6-Unit Walkup",
    "Seaport District Class A Office",
    "Newbury Street Retail & Loft",
    "Jamaica Plain 2-Family Addition",
    "East Boston Mixed-Use Redevelopment",
    "Roxbury Community Center Retrofit",
]


# ---- Persistence ----------------------------------------------------------
def _wipe_boston_projects(conn: psycopg.Connection, boston_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM design_snapshots WHERE project_id IN (
                SELECT id FROM projects WHERE jurisdiction_id = %s
            );
            """,
            (boston_id,),
        )
        cur.execute(
            """
            DELETE FROM compliance_findings WHERE project_id IN (
                SELECT id FROM projects WHERE jurisdiction_id = %s
            );
            """,
            (boston_id,),
        )
        cur.execute(
            """
            DELETE FROM projects WHERE jurisdiction_id = %s;
            """,
            (boston_id,),
        )
    conn.commit()
    print("[boston]   wiped previously-seeded Boston projects/snapshots")


def _insert_projects_and_snapshots(
    conn: psycopg.Connection,
    boston_id: str,
    projects: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    """Insert projects + design snapshots. Returns [(id, name, occupancy_type)]."""
    inserted: list[tuple[str, str, str]] = []
    with conn.cursor() as cur:
        with conn.transaction():
            for p in projects:
                cur.execute(
                    """
                    INSERT INTO projects
                        (name, jurisdiction_id, occupancy_type, metadata)
                    VALUES (%s, %s, %s, %s::jsonb)
                    RETURNING id;
                    """,
                    (
                        p["name"],
                        boston_id,
                        p["occupancy_type"],
                        json.dumps(p["metadata"]),
                    ),
                )
                project_id = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO design_snapshots
                        (project_id, source_agent, payload)
                    VALUES (%s, %s, %s::jsonb);
                    """,
                    (
                        project_id,
                        "seed-pipeline/cad-agent-v1",
                        json.dumps(p["design_snapshot"]),
                    ),
                )
                inserted.append((project_id, p["name"], p["occupancy_type"]))
    conn.commit()
    return inserted


# ---- Main -----------------------------------------------------------------
def main() -> int:
    settings = get_settings()

    try:
        resource_id = _find_resource_id()
        permits = _fetch_permits(resource_id, limit=FETCH_LIMIT)
    except Exception as exc:
        print(f"[boston] ERROR fetching Boston permits: {exc}")
        print("[boston]   Check your internet connection or retry shortly.")
        return 1

    if not permits:
        print("[boston] ERROR: no permits fetched.")
        return 1

    seeds = _pick_project_seeds(permits)
    print(f"[boston] Selected {len(seeds)} permits as project seeds")

    projects = _projects_from_seeds(seeds, PROJECT_NAMES)
    print(f"[boston] Constructed {len(projects)} synthetic projects")

    with psycopg.connect(settings.DATABASE_URL) as conn:
        jurisdiction_ids = _ensure_jurisdictions(conn)
        boston_id = jurisdiction_ids["Boston"]

        _wipe_boston_projects(conn, boston_id)  # idempotent re-runs

        created = _insert_projects_and_snapshots(conn, boston_id, projects)

        print("\n[boston] Summary: projects created")
        for pid, name, occ in created:
            print(f"  {pid}  {occ:50s}  {name}")

        # Show one project with its metadata + snapshot payload
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.name, p.occupancy_type, p.metadata, ds.payload
                  FROM projects p
                  JOIN design_snapshots ds ON ds.project_id = p.id
                 WHERE p.jurisdiction_id = %s
                 ORDER BY p.name
                 LIMIT 1;
                """,
                (boston_id,),
            )
            row = cur.fetchone()
            if row:
                name, occ, meta, payload = row
                print(f"\n[boston] Sample project: {name}")
                print(f"  occupancy_type: {occ}")
                # Truncate long metadata for display
                meta_str = json.dumps(meta, indent=2)
                if len(meta_str) > 1200:
                    meta_str = meta_str[:1200] + "\n... (truncated)"
                print(f"  metadata: {meta_str}")
                payload_str = json.dumps(payload, indent=2)
                if len(payload_str) > 900:
                    payload_str = payload_str[:900] + "\n... (truncated)"
                print(f"  design_snapshot.payload: {payload_str}")

    print("\n[boston] NYC stretch skipped — Boston + CODE-ACCORD is sufficient.")
    print("[boston]   To add later: find the NYC DOB permits Socrata dataset id")
    print("[boston]   via data.cityofnewyork.us and add a second jurisdiction block.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
