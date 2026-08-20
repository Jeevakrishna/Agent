import { NextRequest, NextResponse } from "next/server";

/**
 * GET /api/watch
 *
 * Returns the current session's jurisdiction watch list (empty array if
 * nothing subscribed). Session storage lives in a well-known global Map
 * shared with /api/chat (see also). Demo-only; Step 9 moves to persistent
 * store.
 */

const SESSION_COOKIE = "prca_session_id";

interface SessionState {
  watching: string[];
  last_seen: number;
}

// Next.js server-only modules share module scope within each worker.
// Using globalThis via object-key to avoid TypeScript declaration errors.
const g = globalThis as unknown as {
  __prca_sessions_map?: Map<string, SessionState>;
};

function getSessionsMap(): Map<string, SessionState> {
  if (!g.__prca_sessions_map) {
    g.__prca_sessions_map = new Map<string, SessionState>();
  }
  return g.__prca_sessions_map;
}

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const sessions = getSessionsMap();
  const sid = req.cookies.get(SESSION_COOKIE)?.value;
  let watching: string[] = [];
  if (sid) {
    const s = sessions.get(sid);
    if (s) {
      s.last_seen = Date.now();
      watching = s.watching;
    }
  }
  return NextResponse.json({
    ok: true as const,
    session_id: sid ?? null,
    watching,
  });
}
