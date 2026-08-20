"""Thin psycopg helper — no ORM. Package module at agent.db.

Provides:
  - get_connection() → sync psycopg.Connection using DATABASE_URL
  - search_rules()  → pgvector cosine similarity search over rules.embedding
  - get_jurisdiction_by_name()
  - get_projects_with_latest_design()
  - insert_rule_change()
  - insert_compliance_finding()
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent.config import get_settings


def get_connection() -> psycopg.Connection[dict[str, Any]]:
    """Return a new dict-row psycopg connection from DATABASE_URL.

    Caller is responsible for closing the connection (or use as a context manager).
    """
    settings = get_settings()
    return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)


def _to_pgvector_literal(embedding: list[float]) -> str:
    """Render a list[float] as a pgvector literal, e.g. '[0.1,0.2,...]'.

    Bypasses the need for a separate psycopg vector adapter on the client.
    """
    floats = ",".join(f"{float(v):.10g}" for v in embedding)
    return f"[{floats}]"


# ---------------------------------------------------------------------------
# Jurisdictions
# ---------------------------------------------------------------------------
def get_jurisdiction_by_name(
    name: str, conn: psycopg.Connection | None = None
) -> dict[str, Any] | None:
    """Look up a jurisdiction row by exact name. Returns None if not found."""
    sql = "SELECT id, name, level, parent_id FROM jurisdictions WHERE name = %s LIMIT 1;"
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, (name,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Projects + design snapshots
# ---------------------------------------------------------------------------
def get_projects_with_latest_design(
    jurisdiction_id: str | None = None,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return all projects joined with their most recent design_snapshot.

    Each row is: project_* cols + design_snapshot.payload (as 'design_payload').
    Projects without a design snapshot are still included (design_payload={}).
    """
    sql = """
        WITH ranked AS (
            SELECT
                ds.project_id,
                ds.payload AS design_payload,
                ROW_NUMBER() OVER (
                    PARTITION BY ds.project_id ORDER BY ds.created_at DESC
                ) AS rn
              FROM design_snapshots ds
        )
        SELECT
            p.id            AS project_id,
            p.name          AS project_name,
            p.jurisdiction_id,
            p.occupancy_type AS project_occupancy_type,
            p.metadata      AS project_metadata,
            COALESCE(r.design_payload, '{}'::jsonb) AS design_payload
          FROM projects p
          LEFT JOIN ranked r
                 ON r.project_id = p.id AND r.rn = 1
    """
    args: list[Any] = []
    if jurisdiction_id:
        sql += " WHERE p.jurisdiction_id = %s"
        args.append(jurisdiction_id)
    sql += " ORDER BY p.name;"
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# rule_changes (persist inputs so compliance_findings has valid FK targets)
# ---------------------------------------------------------------------------
def insert_rule_change(
    *,
    rule_id: str | None,
    change_type: str,
    old_text: str | None,
    new_text: str,
    conn: psycopg.Connection | None = None,
) -> str:
    """Insert into rule_changes and return the new rule_changes.id (UUID str)."""
    sql = """
        INSERT INTO rule_changes (rule_id, change_type, old_text, new_text)
        VALUES (%s, %s, %s, %s)
        RETURNING id;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        with c.transaction():
            cur = c.execute(sql, (rule_id, change_type, old_text, new_text))
            rc_id = str(cur.fetchone()["id"])
        c.commit() if own else None
        return rc_id
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# compliance_findings — persist the agent's output
# ---------------------------------------------------------------------------
def insert_compliance_finding(
    *,
    project_id: str,
    rule_change_id: str | None,
    status: str,
    confidence: float,
    explanation: str,
    conn: psycopg.Connection | None = None,
) -> str:
    """Persist a ComplianceFinding to compliance_findings; return the new UUID."""
    sql = """
        INSERT INTO compliance_findings
            (project_id, rule_change_id, status, confidence, explanation)
        VALUES (%s, %s, %s, %s::numeric(3,2), %s)
        RETURNING id;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        with c.transaction():
            cur = c.execute(
                sql,
                (project_id, rule_change_id, status, round(float(confidence), 2), explanation),
            )
            fid = str(cur.fetchone()["id"])
        c.commit() if own else None
        return fid
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


def upsert_compliance_finding(
    *,
    project_id: str,
    rule_change_id: str | None,
    status: str,
    confidence: float,
    explanation: str,
    conn: psycopg.Connection | None = None,
) -> str:
    """Idempotent insert: dedupe on (project_id, rule_change_id, status, explanation) digest.

    If an OPEN (same project_id + rule_change_id + status) finding already exists whose
    explanation matches, return its id instead of inserting a duplicate. Otherwise insert.
    """
    own = conn is None
    try:
        c = conn or get_connection()
        # Look for an identical open finding (same project, same rule_change, same status,
        # same explanation text). If found, just return its id.
        if rule_change_id:
            lookup_sql = """
                SELECT id FROM compliance_findings
                 WHERE project_id = %s
                   AND rule_change_id = %s
                   AND status = %s
                   AND explanation = %s
                 ORDER BY created_at DESC
                 LIMIT 1;
            """
            args = (project_id, rule_change_id, status, explanation)
        else:
            lookup_sql = """
                SELECT id FROM compliance_findings
                 WHERE project_id = %s
                   AND rule_change_id IS NULL
                   AND status = %s
                   AND explanation = %s
                 ORDER BY created_at DESC
                 LIMIT 1;
            """
            args = (project_id, status, explanation)
        cur = c.execute(lookup_sql, args)
        existing = cur.fetchone()
        if existing:
            return str(existing["id"])
        return insert_compliance_finding(
            project_id=project_id,
            rule_change_id=rule_change_id,
            status=status,
            confidence=confidence,
            explanation=explanation,
            conn=c,
        )
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# rule_changes — load persisted rows back into the RuleChange domain model
# ---------------------------------------------------------------------------
def get_rule_change_by_id(
    rule_change_id: str, conn: psycopg.Connection | None = None
) -> dict[str, Any] | None:
    """Return a rule_changes row (+ joined rule.code_section / rule.jurisdiction_id /
    jurisdiction.name / rule.source_url) or None if not found."""
    sql = """
        SELECT
            rc.id           AS rule_change_id,
            rc.rule_id      AS rule_id,
            rc.change_type  AS change_type,
            rc.old_text     AS old_text,
            rc.new_text     AS new_text,
            rc.detected_at  AS detected_at,
            r.code_section  AS code_section,
            r.source_url    AS source_url,
            r.effective_date AS effective_date,
            j.id            AS jurisdiction_id,
            j.name          AS jurisdiction_name,
            j.level         AS jurisdiction_level
          FROM rule_changes rc
          LEFT JOIN rules r       ON r.id = rc.rule_id
          LEFT JOIN jurisdictions j ON j.id = r.jurisdiction_id
         WHERE rc.id = %s
         LIMIT 1;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, (rule_change_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


def get_project_by_id(
    project_id: str, conn: psycopg.Connection | None = None
) -> dict[str, Any] | None:
    """Return a single project row (joined with jurisdiction) or None."""
    sql = """
        SELECT
            p.id, p.name, p.jurisdiction_id, p.occupancy_type, p.metadata,
            j.name AS jurisdiction_name, j.level AS jurisdiction_level
          FROM projects p
          JOIN jurisdictions j ON j.id = p.jurisdiction_id
         WHERE p.id = %s
         LIMIT 1;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, (project_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Rules / jurisdictions lookups used by /ask and by project_id check mode
# ---------------------------------------------------------------------------
def list_rules_for_jurisdiction(
    jurisdiction_id: str,
    limit: int = 500,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return all rules for a jurisdiction (up to `limit`)."""
    sql = """
        SELECT id, code_section, text, effective_date, source_url, created_at
          FROM rules
         WHERE jurisdiction_id = %s
         ORDER BY code_section
         LIMIT %s;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, (jurisdiction_id, limit))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


def list_recent_rule_changes(
    limit: int = 20,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent `limit` rule_changes rows (+ joined rule/code info)."""
    sql = """
        SELECT
            rc.id, rc.rule_id, rc.change_type, rc.old_text, rc.new_text,
            rc.detected_at, r.code_section, r.source_url, j.name AS jurisdiction
          FROM rule_changes rc
          LEFT JOIN rules r ON r.id = rc.rule_id
          LEFT JOIN jurisdictions j ON j.id = r.jurisdiction_id
         ORDER BY rc.detected_at DESC
         LIMIT %s;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


def list_recent_findings(
    limit: int = 20,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent compliance_findings rows."""
    sql = """
        SELECT
            cf.id, cf.project_id, cf.rule_change_id, cf.status, cf.confidence,
            cf.explanation, cf.created_at, p.name AS project_name
          FROM compliance_findings cf
          LEFT JOIN projects p ON p.id = cf.project_id
         ORDER BY cf.created_at DESC
         LIMIT %s;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# compliance_findings listing + filtering for GET /findings endpoint
# ---------------------------------------------------------------------------
def list_findings(
    *,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """List compliance_findings, newest first, optionally filtered."""
    sql = """
        SELECT
            cf.id,
            cf.project_id,
            p.name AS project_name,
            cf.rule_change_id,
            rc.change_type  AS rule_change_type,
            r.code_section  AS rule_code_section,
            j.name          AS rule_jurisdiction,
            cf.status,
            cf.confidence,
            cf.explanation,
            cf.created_at
          FROM compliance_findings cf
          LEFT JOIN projects p      ON p.id  = cf.project_id
          LEFT JOIN rule_changes rc ON rc.id = cf.rule_change_id
          LEFT JOIN rules r         ON r.id  = rc.rule_id
          LEFT JOIN jurisdictions j ON j.id  = r.jurisdiction_id
         WHERE 1=1
    """
    args: list[Any] = []
    if project_id:
        sql += " AND cf.project_id = %s"
        args.append(project_id)
    if status:
        sql += " AND cf.status = %s"
        args.append(status)
    sql += " ORDER BY cf.created_at DESC LIMIT %s OFFSET %s;"
    args.extend([limit, offset])
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


def search_rules(
    query_embedding: list[float],
    jurisdiction_id: str | None = None,
    limit: int = 5,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """Cosine-similarity search over rules using pgvector.

    Parameters
    ----------
    query_embedding:
        384-dim vector from all-MiniLM-L6-v2.
    jurisdiction_id:
        Optional UUID str to restrict the search to a single jurisdiction.
    limit:
        Maximum number of rules to return (default 5).
    conn:
        Optional existing connection. If None a new one is created and closed.

    Returns
    -------
    list[dict]
        Rule rows augmented with a `similarity` column (1 - cosine distance, so
        higher = more similar; 0..1 range when embeddings are unit-normalized).
    """
    if len(query_embedding) != 384:
        raise ValueError(
            f"query_embedding must be 384-dimensional, got {len(query_embedding)}"
        )
    if limit < 1:
        raise ValueError("limit must be >= 1")

    vector_lit = _to_pgvector_literal(query_embedding)

    # Pass the vector literal as a TEXT-typed %s placeholder and cast it to
    # vector(384) inside the query. pgvector accepts a string representation
    # of the array: `'[...]'::text::vector(384)`. Using a %s placeholder keeps
    # us in the safe psycopg parameterisation path while still giving pgvector
    # the stringified-array shape it needs for the `<=>` cosine operator.
    vec_text = vector_lit

    sql = """
        SELECT
            r.id,
            r.jurisdiction_id,
            r.code_section,
            r.text,
            r.effective_date,
            r.superseded_by,
            r.source_url,
            r.created_at,
            1 - (r.embedding <=> %s::text::vector(384)) AS similarity
        FROM rules r
    """
    args: list[Any] = [vec_text]

    if jurisdiction_id is not None:
        sql += " WHERE r.jurisdiction_id = %s\n"
        args.append(jurisdiction_id)

    sql += " ORDER BY r.embedding <=> %s::text::vector(384)\n LIMIT %s;"
    args.extend([vec_text, limit])

    own_conn = conn is None
    active_conn: psycopg.Connection = conn or get_connection()
    try:
        cur = active_conn.execute(sql, tuple(args))
        return [dict(row) for row in cur.fetchall()]
    finally:
        if own_conn:
            active_conn.close()


# ---------------------------------------------------------------------------
# Jurisdiction + Rule + RuleChange combined ingest helpers
# ---------------------------------------------------------------------------
def get_or_create_jurisdiction(
    name: str,
    level: str = "city",
    conn: psycopg.Connection | None = None,
) -> str:
    """Get a jurisdiction by name, or create it if missing. Returns id (UUID str)."""
    if level not in ("country", "state", "county", "city"):
        level = "city"
    own = conn is None
    try:
        c = conn or get_connection()
        with c.transaction():
            cur = c.execute(
                "SELECT id FROM jurisdictions WHERE name = %s LIMIT 1;",
                (name,),
            )
            row = cur.fetchone()
            if row:
                return str(row["id"])
            cur = c.execute(
                "INSERT INTO jurisdictions (name, level) VALUES (%s, %s) RETURNING id;",
                (name, level),
            )
            rc_id = str(cur.fetchone()["id"])
        c.commit() if own else None
        return rc_id
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


def upsert_rule(
    *,
    jurisdiction_id: str,
    code_section: str,
    text: str,
    embedding: list[float],
    source_url: str,
    effective_date: date | None = None,
    conn: psycopg.Connection | None = None,
) -> str:
    """Idempotently upsert a rule row (match by jurisdiction_id + code_section).

    If a matching rule exists, update its text, embedding, effective_date,
    source_url and return the existing id. Otherwise insert a new row.
    """
    own = conn is None
    try:
        c = conn or get_connection()
        vec_text = _to_pgvector_literal(embedding) if embedding else None
        with c.transaction():
            lookup_sql = """
                SELECT id FROM rules
                 WHERE jurisdiction_id = %s AND code_section = %s
                 LIMIT 1;
            """
            cur = c.execute(lookup_sql, (jurisdiction_id, code_section))
            existing = cur.fetchone()
            if existing:
                rule_id = str(existing["id"])
                cur = c.execute(
                    """
                    UPDATE rules SET
                        text = %s,
                        embedding = %s::text::vector(384),
                        effective_date = %s,
                        source_url = %s
                    WHERE id = %s;
                    """,
                    (text, vec_text, effective_date, source_url, rule_id),
                )
            else:
                cur = c.execute(
                    """
                    INSERT INTO rules
                        (jurisdiction_id, code_section, text, embedding,
                         effective_date, source_url)
                    VALUES
                        (%s, %s, %s, %s::text::vector(384), %s, %s)
                    RETURNING id;
                    """,
                    (jurisdiction_id, code_section, text, vec_text, effective_date, source_url),
                )
                rule_id = str(cur.fetchone()["id"])
        c.commit() if own else None
        return rule_id
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


def ingest_rule_change(
    *,
    jurisdiction_name: str,
    code_section: str,
    old_text: str | None,
    new_text: str,
    effective_date: date | None,
    source_url: str,
    change_type: str = "amended",
    embedding: list[float],
    conn: psycopg.Connection | None = None,
) -> str:
    """High-level combined ingest: get/create jurisdiction → upsert rule → insert rule_change.

    Returns the new rule_changes.id. Idempotent across retries (rule upsert +
    rule_change dedup based on same rule_id + same new_text).
    """
    if change_type not in ("new", "amended", "repealed", "clarification"):
        change_type = "amended"
    own = conn is None
    try:
        c = conn or get_connection()
        with c.transaction():
            jurisdiction_id = get_or_create_jurisdiction(jurisdiction_name, conn=c)
            rule_id = upsert_rule(
                jurisdiction_id=jurisdiction_id,
                code_section=code_section,
                text=new_text,
                embedding=embedding,
                source_url=source_url,
                effective_date=effective_date,
                conn=c,
            )
            dedup_sql = """
                SELECT id FROM rule_changes
                 WHERE rule_id = %s AND new_text = %s
                 ORDER BY detected_at DESC LIMIT 1;
            """
            cur = c.execute(dedup_sql, (rule_id, new_text))
            existing_rc = cur.fetchone()
            if existing_rc:
                rc_id = str(existing_rc["id"])
            else:
                cur = c.execute(
                    """
                    INSERT INTO rule_changes
                        (rule_id, change_type, old_text, new_text)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (rule_id, change_type, old_text, new_text),
                )
                rc_id = str(cur.fetchone()["id"])
        c.commit() if own else None
        return rc_id
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Projects listing — used by chat UI /permit-check autocomplete
# ---------------------------------------------------------------------------
def list_projects(
    search: str | None = None,
    jurisdiction_id: str | None = None,
    limit: int = 100,
    conn: psycopg.Connection | None = None,
) -> list[dict[str, Any]]:
    """List projects, optionally filtered by name substring or jurisdiction.

    Returns id, name, jurisdiction_id, jurisdiction_name, occupancy_type,
    metadata — everything the chat UI needs for autocomplete + permit-check.
    """
    sql = """
        SELECT
            p.id,
            p.name,
            p.jurisdiction_id,
            j.name AS jurisdiction_name,
            p.occupancy_type,
            p.metadata
          FROM projects p
          LEFT JOIN jurisdictions j ON j.id = p.jurisdiction_id
         WHERE 1=1
    """
    args: list[Any] = []
    if search:
        sql += " AND p.name ILIKE %s"
        args.append(f"%{search}%")
    if jurisdiction_id:
        sql += " AND p.jurisdiction_id = %s"
        args.append(jurisdiction_id)
    sql += " ORDER BY p.name LIMIT %s;"
    args.append(limit)
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Rule change detail — used by /diff command
# ---------------------------------------------------------------------------
def get_rule_change_detail(
    rule_change_id: str,
    conn: psycopg.Connection | None = None,
) -> dict[str, Any] | None:
    """Return a single rule_change with OLD rule text (if any) + NEW rule text,
    code_section, source_url, effective_date, jurisdiction info.

    Used by the chat /diff [rule_id] command to render before/after.
    """
    sql = """
        SELECT
            rc.id           AS rule_change_id,
            rc.change_type  AS change_type,
            rc.old_text     AS old_text,
            rc.new_text     AS new_text,
            rc.detected_at  AS detected_at,
            r.id            AS rule_id,
            r.code_section  AS code_section,
            r.source_url    AS source_url,
            r.effective_date AS effective_date,
            j.id            AS jurisdiction_id,
            j.name          AS jurisdiction_name,
            j.level         AS jurisdiction_level
          FROM rule_changes rc
          LEFT JOIN rules r       ON r.id = rc.rule_id
          LEFT JOIN jurisdictions j ON j.id = r.jurisdiction_id
         WHERE rc.id = %s
         LIMIT 1;
    """
    own = conn is None
    try:
        c = conn or get_connection()
        cur = c.execute(sql, (rule_change_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        if own:
            (conn or c).close()  # type: ignore[return-value]
