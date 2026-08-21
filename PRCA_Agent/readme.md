# PRCA — Permitting & Regulatory Compliance Agent

A chat-native AI agent that watches building codes, zoning ordinances, and
permit requirements across jurisdictions, and proactively flags which of a
firm's open engineering designs are now non-compliant when a rule changes.

**Every output includes the disclaimer:**  
_"Flagged for review — not a legal compliance determination."_

## Quickstart

### 1. Start the database (Postgres 16 + pgvector)
```bash
docker compose -f infra/docker-compose.yml up -d
```

### 2. Set up the agent service
```bash
cd agent
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -e .
cp .env.example .env
# Edit .env and paste in your free-tier API keys (see comments in .env.example)

# Run the FastAPI service
uvicorn agent.main:app --reload --host 0.0.0.0 --port 8000
# Verify:
# curl http://localhost:8000/health
```

### 3. Start the Next.js web app
```bash
cd app
npm install
npm run dev
# Open http://localhost:3000
```

## Stack

- `/app`   — Next.js 16 (App Router) + TypeScript + Tailwind
- `/agent` — Python 3.11, FastAPI, LangGraph, LangChain
- `/infra` — Docker Compose (Postgres 16 + pgvector), Inngest

