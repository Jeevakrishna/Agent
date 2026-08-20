import { NextRequest, NextResponse } from "next/server";
import type {
  AckPayload,
  AnswerPayload,
  ChatResponseEnvelope,
  DiffPayload,
  ErrorPayload,
  FindingCard,
} from "@/lib/chat-types";

/**
 * POST /api/chat — server-side command validation + dispatch.
 *
 * The chat UI parses /slash-commands client-side for UX autocomplete, but
 * the actual command classification and execution lives HERE so a malicious
 * client can never tell us "we ran a compliance check" without actually
 * talking to the agent API.
 *
 * Supported commands (all are case-insensitive on the /command token):
 *
 *   /permit-check [project-name-or-id]
 *   /watch [jurisdiction-name]
 *   /diff [rule_change_id]
 *   /ask [free text question]
 *
 * Anything starting with `/` that doesn't match one of those returns a
 * 200 with type=error listing the four commands (UX-friendly, not HTTP 400
 * because this is an expected user input path).
 *
 * Messages that do NOT start with `/` are treated as `/ask ...` implicitly
 * so users get free-form answers from a casual prompt.
 */

const AGENT_API_URL = (
  process.env.AGENT_API_URL || "http://localhost:8000"
).replace(/\/+$/, "");

export const dynamic = "force-dynamic";
export const maxDuration = 60;

const KNOWN_COMMANDS = [
  {
    token: "/permit-check",
    description: "Run a full compliance check for a project (by name or id)",
  },
  {
    token: "/watch",
    description: "Subscribe this session to updates from a jurisdiction",
  },
  {
    token: "/diff",
    description: "Show before/after text of a rule change (rule_change_id)",
  },
  {
    token: "/ask",
    description: "Free-form question answered against the rule + project store",
  },
] as const;

const DISCLAIMER =
  "Flagged for review — not a legal compliance determination.";

interface AgentProject {
  id: string;
  name: string;
  jurisdiction_id: string;
  jurisdiction_name?: string | null;
  occupancy_type?: string | null;
  metadata?: unknown | null;
}

interface AgentFinding {
  id?: string | null;
  project_id: string;
  project_name?: string | null;
  rule_change_id?: string | null;
  rule_code_section?: string | null;
  rule_jurisdiction?: string | null;
  status: "compliant" | "flagged" | "needs_review";
  confidence: number;
  explanation: string;
  cited_rule_text?: string | null;
  source_url?: string | null;
  matched_attribute?: string | null;
  created_at?: string | null;
}

interface AgentComplianceResponse {
  run_id?: string;
  mode?: string;
  findings: AgentFinding[];
}

interface AgentAskResponse {
  answer: string;
  sources: Array<{
    title: string;
    url?: string | null;
    snippet?: string | null;
  }>;
}

interface AgentRuleChangeDetail {
  rule_change_id: string;
  rule_id?: string | null;
  code_section?: string | null;
  source_url?: string | null;
  change_type: "new" | "amended" | "repealed" | "clarification";
  old_text?: string | null;
  new_text: string;
  effective_date?: string | null;
  detected_at?: string | null;
  jurisdiction_id?: string | null;
  jurisdiction_name?: string | null;
}

async function agentFetchJson<TResp>(
  path: string,
  options: RequestInit = {},
): Promise<TResp> {
  const url = path.startsWith("http") ? path : `${AGENT_API_URL}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    cache: "no-store",
  });
  const text = await res.text();
  let data: unknown = text;
  try {
    data = text ? (JSON.parse(text) as unknown) : null;
  } catch {
    /* leave raw text */
  }
  if (!res.ok) {
    const detail =
      typeof data === "object" &&
      data !== null &&
      "detail" in data &&
      data.detail &&
      typeof data.detail === "object" &&
      "error" in (data.detail as Record<string, unknown>)
        ? JSON.stringify((data.detail as Record<string, unknown>).error)
        : typeof data === "object" && data !== null && "detail" in data
          ? (data.detail as Record<string, unknown>).toString().slice(0, 400)
          : text.slice(0, 400);
    throw new Error(
      `Agent API ${options.method || "GET"} ${path} returned HTTP ${
        res.status
      }: ${detail || res.statusText}`,
    );
  }
  return data as TResp;
}

function makeErrorEnvelope(
  message: string,
  hint?: string | null,
  includeCommands = true,
): NextResponse<ChatResponseEnvelope> {
  const payload: ErrorPayload = {
    message,
    hint: hint ?? null,
    available_commands: includeCommands
      ? KNOWN_COMMANDS.map((c) => `${c.token} — ${c.description}`)
      : null,
  };
  return NextResponse.json({ type: "error", payload } as ChatResponseEnvelope);
}

function parseCommand(message: string): {
  command: "/permit-check" | "/watch" | "/diff" | "/ask" | "unknown" | "none";
  rest: string;
} {
  const trimmed = message.trimStart();
  if (!trimmed.startsWith("/")) {
    return { command: "/ask", rest: message };
  }
  const [firstWord, ...restTokens] = trimmed.split(/\s+/);
  const lower = firstWord.toLowerCase();
  const rest = restTokens.join(" ").trim();
  switch (lower) {
    case "/permit-check":
    case "/permitcheck":
      return { command: "/permit-check", rest };
    case "/watch":
      return { command: "/watch", rest };
    case "/diff":
      return { command: "/diff", rest };
    case "/ask":
      return { command: "/ask", rest };
    default:
      return { command: "unknown", rest: `${firstWord} ${rest}`.trim() };
  }
}

function normalizeFinding(f: AgentFinding): FindingCard {
  const suggestedAction = (() => {
    if (f.status !== "flagged") return null;
    if (f.matched_attribute) {
      return `Revise design attribute "${f.matched_attribute}" and re-file permit.`;
    }
    return "Review design against the cited rule and prepare a correction memo.";
  })();

  return {
    id: f.id ?? null,
    project_id: f.project_id,
    project_name: f.project_name ?? null,
    rule_change_id: f.rule_change_id ?? null,
    rule_code_section: f.rule_code_section ?? null,
    rule_jurisdiction: f.rule_jurisdiction ?? null,
    status: f.status,
    confidence: typeof f.confidence === "number" ? f.confidence : 0,
    explanation: f.explanation || "(no explanation provided)",
    cited_rule_text: f.cited_rule_text ?? null,
    old_rule_text: null, // populated by /diff command, not compliance-check
    source_url: f.source_url ?? null,
    matched_attribute: f.matched_attribute ?? null,
    suggested_action: suggestedAction,
    disclaimer: DISCLAIMER,
  };
}

async function findProjectByNameOrId(
  input: string,
): Promise<AgentProject | null> {
  const trimmed = input.trim();
  if (!trimmed) return null;
  // 1. Try exact UUID match against /projects/{id} by scanning list with id filter.
  //    We don't have a dedicated GET /projects/:id — fetch list and search.
  const all = await agentFetchJson<AgentProject[]>("/projects");
  const exactId = all.find((p) => p.id.toLowerCase() === trimmed.toLowerCase());
  if (exactId) return exactId;
  // 2. Case-insensitive exact name match.
  const exactName = all.find(
    (p) => p.name.toLowerCase() === trimmed.toLowerCase(),
  );
  if (exactName) return exactName;
  // 3. ILIKE substring — pick the first shortest matching name.
  const needle = trimmed.toLowerCase();
  const matches = all.filter((p) => p.name.toLowerCase().includes(needle));
  matches.sort((a, b) => a.name.length - b.name.length);
  return matches[0] ?? null;
}

// ---------------------------------------------------------------------------
// /watch — per-session in-memory subscription store (demo-only)
// ---------------------------------------------------------------------------
//
// We use an LRU-by-map-order map keyed by an opaque session id we return to
// the client as a signed cookie (not a real JWT — just a random id for demo).
// Step 9 persists this; for now, module-scoped Map resets with hot-reload.
interface SessionState {
  watching: string[];
  last_seen: number;
}
const SESSIONS = new Map<string, SessionState>();

const SESSION_COOKIE = "prca_session_id";

function makeSessionId(): string {
  // 22-char random — Node 18+ + Edge support
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    try {
      return crypto.randomUUID().replace(/-/g, "").slice(0, 22);
    } catch {
      /* fall through */
    }
  }
  return (
    Date.now().toString(36) + Math.random().toString(36).slice(2, 14)
  ).slice(0, 22);
}

function getSession(req: NextRequest): { id: string; new: boolean } {
  const existing = req.cookies.get(SESSION_COOKIE)?.value;
  if (existing) return { id: existing, new: false };
  return { id: makeSessionId(), new: true };
}

function getSessionWatching(sessionId: string): string[] {
  const s = SESSIONS.get(sessionId);
  if (!s) return [];
  s.last_seen = Date.now();
  return s.watching;
}

function addJurisdictionWatch(sessionId: string, jurisdiction: string): string[] {
  const name = jurisdiction.trim();
  if (!name) return getSessionWatching(sessionId);
  const cur = SESSIONS.get(sessionId);
  const list: string[] = cur?.watching ? [...cur.watching] : [];
  if (!list.some((w) => w.toLowerCase() === name.toLowerCase())) {
    list.push(name);
  }
  SESSIONS.set(sessionId, { watching: list, last_seen: Date.now() });
  // Occasional eviction of stale sessions (>2h old)
  const now = Date.now();
  if (SESSIONS.size > 512) {
    for (const [k, v] of SESSIONS) {
      if (now - v.last_seen > 2 * 60 * 60 * 1000) SESSIONS.delete(k);
    }
  }
  return list;
}

// ---------------------------------------------------------------------------
// Route handler
// ---------------------------------------------------------------------------
export async function POST(req: NextRequest) {
  const startedAt = Date.now();
  const { id: sessionId, new: isNewSession } = getSession(req);
  let parsedBody: unknown;
  try {
    parsedBody = await req.json();
  } catch {
    return makeErrorEnvelope("Invalid JSON body.", "Send { message: '...' }", false);
  }
  const body =
    parsedBody && typeof parsedBody === "object"
      ? (parsedBody as { message?: unknown })
      : null;
  const rawMsg = body?.message;
  if (typeof rawMsg !== "string" || rawMsg.trim().length === 0) {
    return makeErrorEnvelope(
      "Empty message.",
      "Send a non-empty string in the `message` field.",
      false,
    );
  }

  const { command, rest } = parseCommand(rawMsg);

  // 200-type response writer that also sets the session cookie if needed.
  const respond = (
    env: ChatResponseEnvelope,
  ): NextResponse<ChatResponseEnvelope> => {
    env.latency_ms = Date.now() - startedAt;
    const res = NextResponse.json(env);
    if (isNewSession) {
      // Set a 30-day session cookie. HttpOnly, same-site lax.
      res.cookies.set({
        name: SESSION_COOKIE,
        value: sessionId,
        httpOnly: true,
        sameSite: "lax",
        path: "/",
        maxAge: 30 * 24 * 60 * 60,
      });
    }
    return res;
  };

  if (command === "unknown") {
    // Extract the bad token for the hint
    const bad = (rawMsg.trim().split(/\s+/)[0] || "?" as string);
    return respond({
      type: "error",
      payload: {
        message: `Unknown command: ${bad as string}`,
        hint: "Commands are parsed server-side. Use one of the four slash commands below or plain text to /ask.",
        available_commands: KNOWN_COMMANDS.map(
          (c) => `${c.token} — ${c.description}`,
        ),
      } satisfies ErrorPayload,
    });
  }

  try {
    if (command === "/permit-check") {
      const arg = rest.trim();
      if (!arg) {
        return makeErrorEnvelope(
          "/permit-check needs a project name or id.",
          "Example: /permit-check Harbor Point Residences",
        );
      }
      const project = await findProjectByNameOrId(arg);
      if (!project) {
        return makeErrorEnvelope(
          `No project found for "${arg}".`,
          "Try the full project name from the seeded list (case-insensitive substring match works).",
          false,
        );
      }
      const agentResp = await agentFetchJson<AgentComplianceResponse>(
        "/compliance-check",
        { method: "POST", body: JSON.stringify({ project_id: project.id }) },
      );
      const findings = (agentResp.findings || []).map(normalizeFinding);
      return respond({
        type: "findings",
        payload: {
          findings,
          // Attach a tiny "scoped project" context the UI can render in the header
        } as { findings: FindingCard[]; _project?: { name: string; id: string } },
      } as unknown as ChatResponseEnvelope);
    }

    if (command === "/watch") {
      const arg = rest.trim();
      if (!arg) {
        return makeErrorEnvelope(
          "/watch needs a jurisdiction name.",
          'Example: /watch Boston',
          false,
        );
      }
      const watching = addJurisdictionWatch(sessionId, arg);
      return respond({
        type: "ack",
        payload: {
          message: `Now watching: ${arg}. You'll see new flags for projects in this jurisdiction here as they arrive.`,
          watching,
        } satisfies AckPayload,
      });
    }

    if (command === "/diff") {
      const arg = rest.trim();
      if (!arg) {
        return makeErrorEnvelope(
          "/diff needs a rule_change_id (UUID).",
          "Example: /diff 550e8400-e29b-41d4-a716-446655440000",
          false,
        );
      }
      let detail: AgentRuleChangeDetail;
      try {
        detail = await agentFetchJson<AgentRuleChangeDetail>(
          `/rule-changes/${encodeURIComponent(arg)}`,
        );
      } catch (e) {
        return makeErrorEnvelope(
          `Could not find rule_change_id="${arg}".`,
          e instanceof Error ? e.message.slice(0, 200) : null,
          false,
        );
      }
      return respond({
        type: "diff",
        payload: {
          rule_change_id: detail.rule_change_id,
          rule_id: detail.rule_id ?? null,
          code_section: detail.code_section ?? null,
          jurisdiction_name: detail.jurisdiction_name ?? null,
          change_type: detail.change_type,
          old_text: detail.old_text ?? null,
          new_text: detail.new_text,
          effective_date: detail.effective_date ?? null,
          source_url: detail.source_url ?? null,
        } satisfies DiffPayload,
      });
    }

    // command === "/ask" — fallthrough from parseCommand for plain text too
    const question = (command === "/ask" ? rest : rawMsg).trim();
    if (!question) {
      return makeErrorEnvelope(
        "Ask needs a question.",
        "Example: /ask What changed for my open projects this month?",
        false,
      );
    }

    // Prefer a dedicated /ask endpoint on the agent if it exists.
    // If it 404s (the minimal agent built in Steps 4-6 doesn't expose one),
    // fall back to building a summary from the recent findings endpoint so
    // we still give a non-empty answer without any additional agent work.
    let agentResp: AgentAskResponse;
    try {
      agentResp = await agentFetchJson<AgentAskResponse>("/ask", {
        method: "POST",
        body: JSON.stringify({
          question,
          session_watching: getSessionWatching(sessionId),
        }),
      });
    } catch (firstErr) {
      // Fallback: summarize recent findings
      void firstErr;
      try {
        const recent = await agentFetchJson<AgentFinding[]>("/findings/recent");
        const flags = recent.filter((f) => f.status === "flagged");
        const compliant = recent.filter((f) => f.status === "compliant");
        const lines: string[] = [];
        if (flags.length === 0) {
          lines.push(
            `As of right now, I don't see any *flagged* compliance findings in the system. ${recent.length} total checks ran recently.`,
          );
        } else {
          lines.push(
            `I found ${flags.length} flagged findings across ${new Set(flags.map((f) => f.project_id)).size} projects. Highest-confidence flags first:`,
          );
          flags
            .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
            .slice(0, 5)
            .forEach((f, i) => {
              lines.push(
                `${i + 1}. **${f.project_name || f.project_id}** vs ${f.rule_code_section || "a rule"} — confidence ${(
                  (f.confidence || 0) * 100
                ).toFixed(0)}%. ${f.explanation.slice(0, 220)}`,
              );
            });
        }
        if (compliant.length > 0) {
          lines.push(
            `\n${compliant.length} other recent checks came back **compliant** — no action needed there.`,
          );
        }
        lines.push(
          "\nUse `/permit-check <project>` for a deep-check on a specific project, or `/diff <rule_change_id>` to see before/after text.",
        );
        agentResp = {
          answer: lines.join("\n"),
          sources: (recent || [])
            .filter((f) => !!f.rule_code_section)
            .map((f) => ({
              title:
                f.rule_code_section ||
                `finding-${(f.id || "").slice(0, 8) as string}`,
              url: f.source_url || null,
              snippet: (f.explanation || "").slice(0, 200),
            }))
            .slice(0, 8),
        };
      } catch (secondErr) {
        return makeErrorEnvelope(
          "Could not reach the agent service for /ask.",
          secondErr instanceof Error ? secondErr.message.slice(0, 200) : null,
          false,
        );
      }
    }

    return respond({
      type: "answer",
      payload: {
        text: agentResp.answer,
        sources: (agentResp.sources || []).map((s) => ({
          title: s.title || "(untitled source)",
          url: s.url ?? null,
          snippet: s.snippet ?? null,
        })),
        disclaimer: DISCLAIMER,
      } satisfies AnswerPayload,
    });
  } catch (err) {
    return makeErrorEnvelope(
      err instanceof Error ? err.message : "Unknown agent error.",
      "This usually means the FastAPI agent service is unreachable. Is it running on " +
        AGENT_API_URL +
        "?",
      false,
    );
  }
}
