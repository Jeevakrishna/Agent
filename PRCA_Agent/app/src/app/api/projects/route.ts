import { NextRequest, NextResponse } from "next/server";

/**
 * GET /api/projects?search=...
 *
 * Proxies to the agent service GET /projects (supports same `search` &
 * `jurisdiction_id` query params). Used by the chat UI slash-command
 * palette to autocomplete /permit-check arguments.
 */
export const dynamic = "force-dynamic";

const AGENT_API_URL = (
  process.env.AGENT_API_URL || "http://localhost:8000"
).replace(/\/+$/, "");

export async function GET(req: NextRequest) {
  const search = req.nextUrl.searchParams.get("search");
  const jid = req.nextUrl.searchParams.get("jurisdiction_id");
  const limit = req.nextUrl.searchParams.get("limit");
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (jid) params.set("jurisdiction_id", jid);
  if (limit) params.set("limit", limit);
  const qs = params.toString();
  const url = `${AGENT_API_URL}/projects${qs ? `?${qs}` : ""}`;
  try {
    const agentResp = await fetch(url, { cache: "no-store" });
    const text = await agentResp.text();
    let data: unknown = text;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      /* leave as text */
    }
    if (!agentResp.ok) {
      return NextResponse.json(
        {
          ok: false as const,
          error: "agent_error",
          detail:
            typeof data === "object" && data !== null && "detail" in data
              ? (data as { detail: unknown }).detail
              : text.slice(0, 300),
        },
        { status: 502 },
      );
    }
    return NextResponse.json({
      ok: true as const,
      projects: (Array.isArray(data) ? data : []) as Array<{
        id: string;
        name: string;
        jurisdiction_id: string;
        jurisdiction_name?: string | null;
        occupancy_type?: string | null;
      }>,
    });
  } catch (e) {
    return NextResponse.json(
      {
        ok: false as const,
        error: "network_error",
        detail: e instanceof Error ? e.message : "Unknown",
      },
      { status: 502 },
    );
  }
}
