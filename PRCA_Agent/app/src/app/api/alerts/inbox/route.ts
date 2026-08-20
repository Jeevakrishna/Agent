import { NextRequest, NextResponse } from "next/server";

/**
 * Simple in-memory alert inbox for Step 6.
 *
 * Step 8 adds realtime push + persistent DB storage. Today:
 *   - POST  /api/alerts/inbox   — append a new alert (from flagRaised function)
 *   - GET   /api/alerts/inbox   — list all pending alerts (newest first)
 *   - DELETE /api/alerts/inbox  — clear all stored alerts
 *
 * Idempotency: dedups POSTs on (finding_id, project_id) so retries from the
 * Inngest step never create duplicate rows.
 */

export interface AlertInboxItem {
  id: string;
  finding_id: string;
  project_id: string;
  event_name: string;
  raised_at: string;
  stored_at: string;
}

/**
 * In-memory store. Module-scoped — this is NOT shared across Next.js server
 * instances and resets on next dev hot-reload. Fine for local demos; replace
 * with a Postgres / Redis store in Step 8.
 */
let STORE: AlertInboxItem[] = [];
const SEEN_KEYS = new Set<string>();

export async function POST(req: NextRequest) {
  try {
    const body = (await req.json()) as Partial<{
      finding_id: string;
      project_id: string;
      event_name: string;
      raised_at: string;
    }>;
    const finding_id = body.finding_id?.trim();
    const project_id = body.project_id?.trim();
    if (!finding_id || !project_id) {
      return NextResponse.json(
        {
          ok: false as const,
          error: "missing_fields",
          detail: "finding_id and project_id are both required",
        },
        { status: 400 },
      );
    }

    const dedupKey = `${finding_id}:${project_id}`;
    if (SEEN_KEYS.has(dedupKey)) {
      const existing = STORE.find(
        (a) => a.finding_id === finding_id && a.project_id === project_id,
      );
      return NextResponse.json({
        ok: true as const,
        deduplicated: true as const,
        stored_at: existing?.stored_at || new Date().toISOString(),
      });
    }

    const item: AlertInboxItem = {
      id: crypto.randomUUID(),
      finding_id,
      project_id,
      event_name: body.event_name?.trim() || "compliance.flag.raised",
      raised_at: body.raised_at?.trim() || new Date().toISOString(),
      stored_at: new Date().toISOString(),
    };
    STORE.unshift(item);
    SEEN_KEYS.add(dedupKey);

    return NextResponse.json({
      ok: true as const,
      deduplicated: false as const,
      stored_at: item.stored_at,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "unknown";
    return NextResponse.json(
      { ok: false as const, error: "bad_json", detail: msg },
      { status: 400 },
    );
  }
}

export async function GET() {
  return NextResponse.json({
    ok: true as const,
    count: STORE.length,
    alerts: STORE,
  });
}

export async function DELETE() {
  STORE = [];
  SEEN_KEYS.clear();
  return NextResponse.json({
    ok: true as const,
    cleared: true as const,
    at: new Date().toISOString(),
  });
}
