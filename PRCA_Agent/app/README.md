# PRCA Agent — Compliance Chat (Next.js)

A B2B engineering chat surface for the PRCA (Project Regulatory Compliance Agent). Users interact with a single conversational UI to run compliance checks, watch jurisdictions for new rule changes, diff before/after rule text, and ask free-form questions — all proxied through structured JSON to the FastAPI agent service.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (Next.js App Router + Tailwind)                     │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ ChatUI.tsx  │  │ MessageBubble│  │ CommandPalette    │  │
│  │ (state mgr) │  │ (renderers)  │  │ (slash autocomplete)│ │
│  └──────┬──────┘  └──────┬───────┘  └───────────────────┘  │
│         │                │                                   │
│         ▼                ▼                                   │
│  ┌─────────────────────────────────────────────┐            │
│  │  POST /api/chat  (server-side validation)   │            │
│  │  - parses slash commands                    │            │
│  │  - dispatches to agent API                  │            │
│  │  - returns structured JSON envelope         │            │
│  └──────────────────┬──────────────────────────┘            │
│                     │                                       │
│                     ▼                                       │
│  ┌─────────────────────────────────────────────┐            │
│  │  GET /api/alerts/stream  (SSE)              │            │
│  │  - pushes compliance.flag.raised in realtime│            │
│  │  - falls back to polling if SSE fails       │            │
│  └─────────────────────────────────────────────┘            │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  FastAPI Agent Service (port 8000)                           │
│  - /compliance-check  — runs the LangGraph agent            │
│  - /ask               — free-form Q&A                        │
│  - /projects          — project list + search                │
│  - /rule-changes/{id} — rule change detail                   │
│  - /findings          — recent findings                      │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  Inngest (scheduled + event-driven)                          │
│  - pollRegulatorySources  — cron, reads /data/incoming/*.json│
│  - onRegulatoryChange     — runs compliance graph            │
│  - onDesignUpdated        — re-checks on design change       │
│  - flagRaised             — stores alert + pushes to SSE bus │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

| Service | Purpose | Default Port |
|---------|---------|--------------|
| Node.js 18+ | Next.js runtime | — |
| FastAPI agent | Compliance graph, LLM, DB | 8000 |
| PostgreSQL + pgvector | Project + rule storage | 5432 |
| Inngest CLI | Local event scheduler | 8288 |
| Ollama (optional) | Local LLM fallback | 11434 |

## Environment Variables

Copy `.env.local` from the example and fill in values:

```bash
# .env.local
AGENT_API_URL=http://localhost:8000
NEXT_PUBLIC_APP_URL=http://localhost:3000
INNGEST_EVENT_KEY=...
INNGEST_SIGNING_KEY=...
```

## Getting Started

```bash
# 1. Install dependencies
cd app
npm install

# 2. Start the Next.js dev server
npm run dev

# 3. In a second terminal, start the agent API
cd agent
.venv\Scripts\python.exe -m agent.main

# 4. In a third terminal, start Inngest dev
npx inngest-cli@latest dev
```

Open [http://localhost:3000](http://localhost:3000).

## Chat Commands

Type `/` in the input to open the command palette. All commands are validated server-side in `POST /api/chat`.

| Command | Example | What it does |
|---------|---------|--------------|
| `/permit-check [project]` | `/permit-check Harbor Point` | Runs full compliance check for a project. Returns structured findings with status badges. |
| `/watch [jurisdiction]` | `/watch Boston` | Subscribes this session to a jurisdiction. New flags for that jurisdiction appear as push alerts. |
| `/diff [rule_change_id]` | `/diff 550e8400-...` | Shows before/after text of a rule change in a two-column layout. |
| `/ask [free text]` | `/ask What changed this month?` | Free-form Q&A. Falls back to summarizing recent findings if no `/ask` endpoint exists. |

Plain text (no leading `/`) is treated as `/ask` implicitly.

## Real-Time Alerts (Step 8)

### How it works

1. **Inngest** detects a regulatory change (cron or event trigger).
2. The `onRegulatoryChange` function runs the compliance graph.
3. For each high-confidence flagged finding, `flagRaised` fires `compliance.flag.raised`.
4. `flagRaised` does two things:
   - Persists the alert to `/api/alerts/inbox` (existing behavior).
   - Publishes to the **in-memory alert bus** (`src/lib/alertBus.ts`).
5. The **SSE stream** (`/api/alerts/stream`) fans out to all connected browser clients.
6. The chat UI subscribes via `EventSource` on mount. When an alert arrives:
   - If the user is watching the finding's jurisdiction (or has no watch filter), the alert card is appended to the chat.
   - If the user has scrolled up, a subtle toast appears: "New regulatory alert".

### Graceful degradation

If SSE fails after 2 connection attempts, the UI falls back to **polling** `GET /api/alerts/inbox` every 5 seconds. A console warning is logged. The chat never breaks over push failing.

### Production note

The in-memory bus (`src/lib/alertBus.ts`) is isolated in one module. Swap it for Redis pub/sub or Inngest realtime by changing that single file.

## Verification Steps

1. **Open the chat** at `http://localhost:3000`.
2. **Run `/watch Boston`** — you should see a "Watching: Boston" chip in the header.
3. **Trigger a rule change**:
   - Drop a JSON file into `/data/incoming/` (see `example-fire-rating-amendment.json.example`), or
   - Re-trigger `pollRegulatorySources` from the Inngest dashboard.
4. **Observe the alert** — without touching the browser, a structured finding card should appear in the chat within a few seconds.
5. **Test polling fallback** — in DevTools Network tab, block `EventSource` requests. The console should show a warning, and alerts should still arrive via polling.

## Project Structure

```
app/
├── src/
│   ├── app/
│   │   ├── api/
│   │   │   ├── alerts/
│   │   │   │   ├── inbox/route.ts      # In-memory alert inbox (POST/GET/DELETE)
│   │   │   │   └── stream/route.ts     # SSE stream for realtime alerts
│   │   │   ├── chat/route.ts           # Server-side command validation + dispatch
│   │   │   ├── projects/route.ts       # Proxy to agent /projects
│   │   │   └── watch/route.ts          # Session watch state (GET)
│   │   ├── globals.css                 # Tailwind + theme vars
│   │   ├── layout.tsx                  # Root layout
│   │   └── page.tsx                    # Chat surface (uses ChatUI)
│   ├── components/
│   │   └── chat/
│   │       ├── ChatUI.tsx              # Main chat container + state
│   │       ├── CommandPalette.tsx      # Slash-command autocomplete
│   │       ├── DiffView.tsx            # Before/after rule diff
│   │       ├── FindingCard.tsx         # Structured finding with status badge
│   │       ├── MessageBubble.tsx       # Message renderer (per type)
│   │       ├── SourcesList.tsx         # Collapsible sources for /ask
│   │       └── WatchChip.tsx           # "Watching: ..." header chip
│   ├── inngest/
│   │   ├── client.ts                   # Inngest client + event names
│   │   └── functions.ts                # Inngest functions (poll, check, flag)
│   └── lib/
│       ├── alertBus.ts                 # In-memory SSE alert bus
│       └── chat-types.ts               # Shared TypeScript types
├── .env.local                          # App secrets (not committed)
├── package.json
└── tsconfig.json
```

## Design Decisions

- **No component library**: Tailwind-only to keep the bundle small and avoid paid dependencies.
- **Server-side command validation**: The client parses `/` for UX autocomplete, but the server in `POST /api/chat` is the source of truth. A malicious client cannot fake a compliance check.
- **Structured JSON everywhere**: The UI never parses free-form text. Every response has a `type` discriminator (`findings`, `answer`, `diff`, `ack`, `error`) and the UI renders from typed fields.
- **In-memory is fine for demo**: The alert bus, session store, and inbox are all module-scoped Maps. They reset on hot-reload. Step 9 would move these to Postgres/Redis.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Chat shows "Unknown agent error" | Ensure the FastAPI agent is running on `AGENT_API_URL` (default `http://localhost:8000`). |
| No alerts appear | Check Inngest CLI is running (`npx inngest-cli@latest dev`). Check `/api/alerts/stream` returns 200. |
| `/watch` chip disappears on refresh | Session cookie is HttpOnly 30-day. If cookies are blocked, the session resets. |
| `/diff` shows empty old text | The rule change may be `new` (no prior version). The UI shows "(no prior version — new rule)". |
