"""Seed CODE-ACCORD regulatory rules + jurisdictions.

Source: https://github.com/Accord-Project/CODE-ACCORD  (MIT / open data)
The dataset is a local clone at CODE-ACCORD-main/ (gitignored). If it does
not exist, the script gives exact instructions to clone it.

Data files used (relative to the local clone):
  annotated_data/entities/all.csv
  annotated_data/relations/all.csv

Columns in entities/all.csv (documented in the repo README):
  example_id, content, processed_content, label, metadata
metadata field is a dict-encoded string with an 'ID' key encoding the
source document, e.g. "69_Finnish_FireSafety" or "5_UK_DocB_Structure".

The jurisdiction split is inferred from the metadata ID prefix/suffix:
  contains "_Finnish_" -> Finland
  contains "_UK_" or "Doc" or "Approved_Document" -> England
This matches the 2-country split described in the CODE-ACCORD paper.
"""

from __future__ import annotations

import ast
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from tenacity import retry, stop_after_attempt, wait_fixed

import psycopg

# Ensure the package is importable whether this runs as a script or as a module.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from agent.config import get_settings  # noqa: E402
from agent.embeddings import embed  # noqa: E402

REPO_ROOT = _THIS_DIR.parent
LOCAL_CLONE = REPO_ROOT / "CODE-ACCORD-main"
ENTITIES_CSV = LOCAL_CLONE / "annotated_data" / "entities" / "all.csv"
RELATIONS_CSV = LOCAL_CLONE / "annotated_data" / "relations" / "all.csv"

CODE_ACCORD_REPO_URL = "https://github.com/Accord-Project/CODE-ACCORD"
ENTITIES_RAW_URL = (
    "https://raw.githubusercontent.com/Accord-Project/CODE-ACCORD/main/"
    "annotated_data/entities/all.csv"
)
RELATIONS_RAW_URL = (
    "https://raw.githubusercontent.com/Accord-Project/CODE-ACCORD/main/"
    "annotated_data/relations/all.csv"
)

SOURCE_URL_FOR_FILE = {
    "entities": (
        "https://github.com/Accord-Project/CODE-ACCORD/blob/main/"
        "annotated_data/entities/all.csv"
    ),
    "relations": (
        "https://github.com/Accord-Project/CODE-ACCORD/blob/main/"
        "annotated_data/relations/all.csv"
    ),
}

# Jurisdictions
JURISDICTIONS = [
    ("England", "country"),
    ("Finland", "country"),
]


def _ensure_local_clone() -> Path:
    """Return the path to a usable CODE-ACCORD clone.

    Preference order:
      1. Existing local clone at CODE-ACCORD-main/
      2. Fall back: download the two CSV files via HTTP into data/
    """
    global ENTITIES_CSV, RELATIONS_CSV
    if ENTITIES_CSV.exists() and RELATIONS_CSV.exists():
        print(f"[code-accord] Using local clone at {LOCAL_CLONE}")
        return LOCAL_CLONE

    # Fallback: download via raw GitHub URLs into REPO_ROOT/data
    fallback_dir = REPO_ROOT / "data" / "code-accord"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    fallback_entities = fallback_dir / "entities_all.csv"
    fallback_relations = fallback_dir / "relations_all.csv"

    _download_csv_if_missing(fallback_entities, ENTITIES_RAW_URL)
    _download_csv_if_missing(fallback_relations, RELATIONS_RAW_URL)

    # Patch the module-level paths to point at the downloaded copies
    ENTITIES_CSV = fallback_entities
    RELATIONS_CSV = fallback_relations
    print(f"[code-accord] Using downloaded CSVs at {fallback_dir}")
    return fallback_dir


@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def _download_csv_if_missing(target: Path, url: str) -> None:
    if target.exists() and target.stat().st_size > 10_000:
        return
    import httpx

    timeout = httpx.Timeout(30.0, connect=10.0)
    print(f"[code-accord] Downloading {url} ...")
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        target.write_bytes(resp.content)
    print(f"[code-accord]   -> {target} ({target.stat().st_size} bytes)")


def _parse_metadata(raw: str) -> dict[str, Any]:
    """The CSV stores metadata as a Python dict-literal string."""
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}


def _jurisdiction_for_doc_id(doc_id: str) -> str:
    """Return the jurisdiction name for a CODE-ACCORD metadata ID."""
    if "_Finnish_" in doc_id:
        return "Finland"
    # Anything matching the UK / Approved Document naming convention
    if "_UK_" in doc_id or "Doc" in doc_id or "Approved_Document" in doc_id:
        return "England"
    # Default: England — UK docs often come through without a UK prefix
    return "England"


def _load_entities() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    print(f"[code-accord] Parsing {ENTITIES_CSV}")
    with open(ENTITIES_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        print(f"[code-accord]   columns: {cols}")
        for row in reader:
            content = (row.get("content") or "").strip()
            if not content:
                continue
            meta = _parse_metadata(row.get("metadata", "{}"))
            doc_id = str(meta.get("ID", "unknown"))
            rows.append(
                {
                    "example_id": row.get("example_id"),
                    "text": content,
                    "doc_id": doc_id,
                    "jurisdiction": _jurisdiction_for_doc_id(doc_id),
                    "metadata": meta,
                }
            )
    print(f"[code-accord]   parsed {len(rows)} entity-annotated sentences")
    return rows


def _batch_embed(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Embed a list of texts in batches using the local sentence transformer."""
    all_vectors: list[list[float]] = []
    total = len(texts)
    for start in range(0, total, batch_size):
        batch = texts[start : start + batch_size]
        vectors = embed(batch)
        all_vectors.extend(vectors)
        done = min(start + batch_size, total)
        print(f"[code-accord]   embedded {done}/{total} texts")
    return all_vectors


def _ensure_jurisdictions(conn: psycopg.Connection) -> dict[str, str]:
    """Ensure both countries exist; return {name: id}."""
    result: dict[str, str] = {}
    with conn.cursor() as cur:
        for name, level in JURISDICTIONS:
            cur.execute(
                """
                INSERT INTO jurisdictions (name, level)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id;
                """,
                (name, level),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT id FROM jurisdictions WHERE name = %s AND level = %s;",
                    (name, level),
                )
                row = cur.fetchone()
            result[name] = row[0]
    conn.commit()
    return result


def _truncate_code_accord_rules(conn: psycopg.Connection) -> None:
    """Remove any previously-seeded CODE-ACCORD rules so reruns are idempotent.

    We identify them by source_url pointing at the CODE-ACCORD repo or
    having code_section starting with "CODE-ACCORD:".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM compliance_findings
             WHERE rule_change_id IN (
                 SELECT rc.id FROM rule_changes rc
                  JOIN rules r ON r.id = rc.rule_id
                  WHERE r.source_url LIKE '%Accord-Project/CODE-ACCORD%'
             );
            DELETE FROM rule_changes
             WHERE rule_id IN (
                 SELECT id FROM rules
                  WHERE source_url LIKE '%Accord-Project/CODE-ACCORD%'
             );
            DELETE FROM rules
             WHERE source_url LIKE '%Accord-Project/CODE-ACCORD%';
            """
        )
    conn.commit()
    print("[code-accord]   wiped previously-seeded CODE-ACCORD rows (if any)")


def _vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.10g}" for x in v) + "]"


def _insert_rules(
    conn: psycopg.Connection,
    jurisdiction_ids: dict[str, str],
    rows: list[dict[str, Any]],
    vectors: list[list[float]],
) -> dict[str, int]:
    counts: dict[str, int] = Counter()
    inserted = 0
    batch_args: list[tuple] = []

    source_url = SOURCE_URL_FOR_FILE["entities"]

    for row, vec in zip(rows, vectors):
        j_name = row["jurisdiction"]
        j_id = jurisdiction_ids[j_name]
        code_section = f"CODE-ACCORD:{row['doc_id']}"
        batch_args.append(
            (
                j_id,
                code_section,
                row["text"],
                _vector_literal(vec),
                source_url,
            )
        )
        counts[j_name] += 1

    insert_sql = """
        INSERT INTO rules
          (jurisdiction_id, code_section, text, embedding, source_url)
        VALUES (%s, %s, %s, %s::vector, %s);
    """
    with conn.cursor() as cur:
        with conn.transaction():
            for args in batch_args:
                cur.execute(insert_sql, args)
                inserted += 1
    conn.commit()
    print(f"[code-accord]   inserted {inserted} rules total")
    return dict(counts)


def _print_examples(conn: psycopg.Connection, n: int = 3) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, code_section, left(text, 120), source_url
              FROM rules
             WHERE source_url LIKE '%%Accord-Project/CODE-ACCORD%%'
             ORDER BY code_section
             LIMIT %s;
            """,
            (n,),
        )
        print(f"\n[code-accord] Example rules ({n}):")
        for row in cur.fetchall():
            rid, section, snippet, url = row
            print(f"  rule.id        = {rid}")
            print(f"  code_section   = {section}")
            print(f"  text (snippet) = {snippet}...")
            print(f"  source_url     = {url}\n")


def main() -> int:
    settings = get_settings()

    try:
        _ensure_local_clone()
    except Exception as exc:
        print(f"[code-accord] ERROR fetching dataset: {exc}")
        print("")
        print("Clone it manually with:")
        print(f"  cd {REPO_ROOT}")
        print(f"  git clone {CODE_ACCORD_REPO_URL}.git CODE-ACCORD-main")
        return 1

    rows = _load_entities()
    if not rows:
        print("[code-accord] ERROR: no rows loaded from CSV.")
        return 1

    with psycopg.connect(settings.DATABASE_URL) as conn:
        jurisdiction_ids = _ensure_jurisdictions(conn)
        print(f"[code-accord] jurisdictions: {jurisdiction_ids}")

        _truncate_code_accord_rules(conn)

        texts = [r["text"] for r in rows]
        print(f"[code-accord] Generating embeddings for {len(texts)} texts...")
        vectors = _batch_embed(texts, batch_size=64)
        assert len(vectors) == len(texts)

        counts = _insert_rules(conn, jurisdiction_ids, rows, vectors)

        print("\n[code-accord] Summary:")
        for j_name, j_id in jurisdiction_ids.items():
            print(f"  {j_name}: {counts.get(j_name, 0)} rules inserted")
        print(f"  grand total: {sum(counts.values())}")

        _print_examples(conn, n=3)

    return 0


if __name__ == "__main__":
    sys.exit(main())
