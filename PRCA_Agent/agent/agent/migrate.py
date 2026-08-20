"""PRCA migration runner — package module at agent.migrate.

Applies SQL files from infra/migrations/ in numeric order and tracks
them in the `schema_migrations` table. Idempotent — safe to run any
number of times.

Usage:
    python agent/migrate.py        # from repo root
    python -m agent.migrate        # if the package is on sys.path
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

import psycopg

from agent.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "migrations"

FILENAME_RE = re.compile(r"^(\d+)_.+\.sql$")


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Return (version, path) tuples sorted ascending by version."""
    discovered: list[tuple[int, Path]] = []
    if not migrations_dir.is_dir():
        return discovered
    for entry in migrations_dir.iterdir():
        if not entry.is_file():
            continue
        match = FILENAME_RE.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        discovered.append((version, entry))
    discovered.sort(key=lambda pair: pair[1].name)
    return discovered


def _ensure_schema_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version integer PRIMARY KEY,
            name text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    conn.commit()


def _applied_versions(conn: psycopg.Connection) -> set[int]:
    cur = conn.execute("SELECT version FROM schema_migrations;")
    return {row[0] for row in cur.fetchall()}


def _read_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _apply_migration(
    conn: psycopg.Connection, version: int, path: Path
) -> None:
    sql = _read_sql(path)
    with conn.transaction():
        conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s);",
            (version, path.name),
        )


def run_migrations(
    dsn: str | None = None,
    migrations_dir: Path | None = None,
) -> Iterable[str]:
    """Apply any outstanding migrations. Yields status strings for logging."""
    dsn = dsn or get_settings().DATABASE_URL
    migrations_dir = migrations_dir or MIGRATIONS_DIR

    all_migrations = _discover_migrations(migrations_dir)
    if not all_migrations:
        yield f"No migration files found in {migrations_dir}"
        return

    yield f"Discovered {len(all_migrations)} migration file(s) in {migrations_dir}"

    with psycopg.connect(dsn) as conn:
        conn.autocommit = False
        _ensure_schema_migrations_table(conn)
        applied = _applied_versions(conn)

        pending = [(v, p) for (v, p) in all_migrations if v not in applied]
        yield f"Already applied: {len(applied)}; Pending: {len(pending)}"

        if not pending:
            yield "Nothing to do — schema is up to date."
            return

        for version, path in pending:
            try:
                _apply_migration(conn, version, path)
                conn.commit()
                yield f"  APPLIED {path.name} (version {version})"
            except Exception as exc:
                conn.rollback()
                yield f"  FAILED  {path.name} (version {version}): {exc}"
                raise

        yield f"Done. Applied {len(pending)} migration(s)."


def main() -> int:
    try:
        for line in run_migrations():
            print(line)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
