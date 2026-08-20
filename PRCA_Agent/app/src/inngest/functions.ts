import fs from "node:fs";
import path from "node:path";
import { inngest, EVENTS } from "./client";
import { publish, type AlertPayload } from "@/lib/alertBus";

/**
 * Agent API base URL — comes from env so it's easy to swap local / staging / prod.
 * Default: the FastAPI service on port 8000.
 */
const AGENT_API_URL =
  (process.env.AGENT_API_URL || "http://localhost:8000").replace(/\/+$/, "");

/**
 * Folder-poller data directory.
 *
 * The Next.js app runs from `/app` (package.json location). The demo watches
 * `../data/incoming` (monorepo-root `/data/incoming`). Processed files are
 * moved to `../data/incoming/processed/`.
 *
 * NOTE: Real deployments would swap the folder poller below for
 * per-jurisdiction scrapers / API pollers behind the SAME
 * `regulatory.change.detected` event — downstream functions never need to
 * know where the rule change came from.
 */
const DEFAULT_INCOMING_DIR = path.resolve(
  process.cwd(),
  "..",
  "data",
  "incoming",
);
const INCOMING_DIR = process.env.INCOMING_DATA_DIR || DEFAULT_INCOMING_DIR;
const PROCESSED_DIR = path.join(INCOMING_DIR, "processed");

/** Shared fetch helper — throws on non-ok so Inngest step retries. */
async function postJson<TResp = unknown>(
  url: string,
  body: unknown,
): Promise<TResp> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let data: unknown = text;
  try {
    data = text ? (JSON.parse(text) as unknown) : null;
  } catch {
    // Leave as text.
  }
  if (!res.ok) {
    const detail =
      typeof data === "object" && data !== null && "detail" in data
        ? JSON.stringify((data as { detail: unknown }).detail)
        : text.slice(0, 400);
    throw new Error(
      `POST ${url} failed: HTTP ${res.status} ${res.statusText} — ${detail}`,
    );
  }
  return data as TResp;
}

export interface IngestRuleChangePayload {
  jurisdiction: string;
  code_section: string;
  old_text?: string | null;
  new_text: string;
  effective_date?: string | null;
  source_url: string;
  change_type?: "new" | "amended" | "repealed" | "clarification" | null;
}

export interface IngestRuleChangeResponse {
  rule_change_id: string;
  rule_id: string;
  jurisdiction_id: string;
  change_type: "new" | "amended" | "repealed" | "clarification";
  ingested_at: string;
}

export interface FindingShape {
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

export interface ComplianceCheckResponse {
  run_id: string;
  duration_ms: number;
  mode: string;
  findings: FindingShape[];
}

/**
 * 1. pollRegulatorySources — cron function (0 6 * * * = 06:00 daily).
 *
 * For the demo: reads JSON files from /data/incoming/, POSTs each one to
 * the agent API at /ingest-rule-change, then emits
 * "regulatory.change.detected" for each ingested rule_change_id. Processed
 * files are moved to /data/incoming/processed/ so re-runs are idempotent.
 *
 * Inngest steps make the file-by-file processing retriable independently:
 * if file 3 fails, files 1+2 don't re-process (they've already been moved).
 */
export const pollRegulatorySources = inngest.createFunction(
  {
    id: "poll-regulatory-sources",
    name: "Poll regulatory sources (folder watcher demo)",
    retries: 2,
    triggers: [{ cron: "0 6 * * *" }],
  },
  async (ctx) => {
    // Manual invocations (from Inngest dashboard) come through the cron
    // trigger too — ctx.event is always populated.
    void ctx.event;

    await ctx.step.run("ensure-incoming-directories", async () => {
      await fs.promises.mkdir(INCOMING_DIR, { recursive: true });
      await fs.promises.mkdir(PROCESSED_DIR, { recursive: true });
      return { incoming_dir: INCOMING_DIR, processed_dir: PROCESSED_DIR };
    });

    const entries = await ctx.step.run("list-incoming-files", async () => {
      const all = await fs.promises.readdir(INCOMING_DIR, {
        withFileTypes: true,
      });
      const files = all
        .filter(
          (d) =>
            d.isFile() &&
            d.name.endsWith(".json") &&
            !d.name.startsWith("."),
        )
        .map((d) => d.name)
        .sort();
      return { count: files.length, files };
    });

    if (entries.count === 0) {
      return { result: "no_new_files", ingested: [] as string[] };
    }

    const ingestedIds: string[] = [];

    for (const fileName of entries.files) {
      const filePath = path.join(INCOMING_DIR, fileName);
      const parsed = await ctx.step.run(
        `read-and-parse:${fileName}`,
        async () => {
          const raw = await fs.promises.readFile(filePath, "utf-8");
          const payload = JSON.parse(raw) as IngestRuleChangePayload;
          if (!payload || typeof payload !== "object") {
            throw new Error(`File ${fileName}: root must be a JSON object`);
          }
          if (!payload.jurisdiction || !payload.code_section) {
            throw new Error(
              `File ${fileName}: missing jurisdiction / code_section`,
            );
          }
          if (!payload.new_text || !payload.source_url) {
            throw new Error(
              `File ${fileName}: missing new_text / source_url`,
            );
          }
          return payload;
        },
      );

      const ingestResp = await ctx.step.run(
        `ingest-rule-change:${fileName}`,
        async () => {
          return postJson<IngestRuleChangeResponse>(
            `${AGENT_API_URL}/ingest-rule-change`,
            parsed,
          );
        },
      );

      ingestedIds.push(ingestResp.rule_change_id);

      await ctx.step.run(
        `emit-regulatory-change-detected:${fileName}`,
        async () => {
          await inngest.send({
            name: EVENTS.REGULATORY_CHANGE_DETECTED,
            data: { rule_change_id: ingestResp.rule_change_id },
          });
          return {
            event_sent: true,
            rule_change_id: ingestResp.rule_change_id,
          };
        },
      );

      await ctx.step.run(`move-to-processed:${fileName}`, async () => {
        const dest = path.join(PROCESSED_DIR, `${Date.now()}-${fileName}`);
        await fs.promises.rename(filePath, dest);
        return { moved_to: dest };
      });
    }

    return {
      result: "ingested",
      count: ingestedIds.length,
      rule_change_ids: ingestedIds,
    };
  },
);

/**
 * 2. onRegulatoryChange — runs the compliance graph for a newly-detected
 *    rule change and emits "compliance.flag.raised" for each high-confidence
 *    "flagged" finding.
 *
 * Uses Inngest steps so each part retries independently (idempotent via
 * the agent's upsert_compliance_finding + confidence threshold filter).
 */
export const onRegulatoryChange = inngest.createFunction(
  {
    id: "on-regulatory-change",
    name: "Run compliance check on regulatory change",
    retries: 3,
    triggers: [{ event: EVENTS.REGULATORY_CHANGE_DETECTED }],
  },
  async (ctx) => {
    const eventData = ctx.event.data as { rule_change_id?: string };
    const rule_change_id = eventData.rule_change_id;
    if (!rule_change_id) {
      throw new Error("regulatory.change.detected missing data.rule_change_id");
    }

    const checkResp = await ctx.step.run(
      "call-agent-compliance-check",
      async () => {
        return postJson<ComplianceCheckResponse>(
          `${AGENT_API_URL}/compliance-check`,
          { rule_change_id },
        );
      },
    );

    const flagged = checkResp.findings.filter(
      (f: FindingShape) => f.status === "flagged" && f.confidence >= 0.8,
    );

    const emitted: string[] = [];
    for (const finding of flagged) {
      if (!finding.id) continue;
      await ctx.step.run(
        `emit-flag-raised:${finding.project_id}:${finding.id}`,
        async () => {
          await inngest.send({
            name: EVENTS.COMPLIANCE_FLAG_RAISED,
            data: {
              finding_id: finding.id as string,
              project_id: finding.project_id,
            },
          });
          emitted.push(finding.id as string);
          return { finding_id: finding.id, project_id: finding.project_id };
        },
      );
    }

    return {
      rule_change_id,
      total_findings: checkResp.findings.length,
      flagged_count: flagged.length,
      emitted_flag_ids: emitted,
    };
  },
);

/**
 * 3. onDesignUpdated — re-checks an updated project against its existing
 *    jurisdiction rules. Any new "flagged" findings (confidence >= 0.8)
 *    raise a "compliance.flag.raised" event.
 */
export const onDesignUpdated = inngest.createFunction(
  {
    id: "on-design-updated",
    name: "Re-check project compliance after design update",
    retries: 3,
    triggers: [{ event: EVENTS.DESIGN_UPDATED }],
  },
  async (ctx) => {
    const eventData = ctx.event.data as { project_id?: string };
    const project_id = eventData.project_id;
    if (!project_id) {
      throw new Error("design.updated missing data.project_id");
    }

    const checkResp = await ctx.step.run(
      "call-agent-compliance-check-project",
      async () => {
        return postJson<ComplianceCheckResponse>(
          `${AGENT_API_URL}/compliance-check`,
          { project_id },
        );
      },
    );

    const flagged = checkResp.findings.filter(
      (f: FindingShape) => f.status === "flagged" && f.confidence >= 0.8,
    );

    const emitted: string[] = [];
    for (const finding of flagged) {
      if (!finding.id) continue;
      await ctx.step.run(
        `emit-flag-raised:${finding.project_id}:${finding.id}`,
        async () => {
          await inngest.send({
            name: EVENTS.COMPLIANCE_FLAG_RAISED,
            data: {
              finding_id: finding.id as string,
              project_id: finding.project_id,
            },
          });
          emitted.push(finding.id as string);
          return { finding_id: finding.id, project_id: finding.project_id };
        },
      );
    }

    return {
      project_id,
      total_findings: checkResp.findings.length,
      flagged_count: flagged.length,
      emitted_flag_ids: emitted,
    };
  },
);

/**
 * 4. flagRaised — for now, persists the finding into a simple in-memory
 *    inbox via /api/alerts/inbox so the frontend can list new alerts.
 *
 * Step 8 adds realtime push; this handler stays stable and just fans out
 * to additional channels (email, Slack, etc.) in the future.
 */
export const flagRaised = inngest.createFunction(
  {
    id: "compliance-flag-raised",
    name: "Store compliance alert in inbox + push to SSE bus",
    retries: 2,
    triggers: [{ event: EVENTS.COMPLIANCE_FLAG_RAISED }],
  },
  async (ctx) => {
    const eventData = ctx.event.data as {
      finding_id?: string;
      project_id?: string;
    };
    const { finding_id, project_id } = eventData;
    if (!finding_id || !project_id) {
      throw new Error(
        "compliance.flag.raised missing data.finding_id / data.project_id",
      );
    }

    const nextApiBase =
      process.env.NEXT_PUBLIC_APP_URL || "http://localhost:3000";

    // 1. Persist to inbox (existing behavior)
    const inboxResp = await ctx.step.run("post-alert-to-inbox", async () => {
      return postJson<{ ok: boolean; stored_at: string }>(
        `${nextApiBase}/api/alerts/inbox`,
        {
          finding_id,
          project_id,
          event_name: EVENTS.COMPLIANCE_FLAG_RAISED,
          raised_at: new Date().toISOString(),
        },
      );
    });

    // 2. Fetch full finding details for the realtime bus
    let findingDetail: {
      project_name?: string | null;
      rule_code_section?: string | null;
      rule_jurisdiction?: string | null;
      status: string;
      confidence: number;
      explanation: string;
      cited_rule_text?: string | null;
      source_url?: string | null;
      matched_attribute?: string | null;
    } | null = null;
    try {
      const findingsRes = await fetch(`${AGENT_API_URL}/findings`, {});
      const allFindings: FindingShape[] = await findingsRes.json();
      findingDetail = allFindings.find((f) => f.id === finding_id) || null;
    } catch {
      /* best-effort; bus publish will use minimal payload */
    }

    // 3. Publish to in-memory alert bus for SSE fan-out
    //    Production: replace with Redis pub/sub or Inngest realtime.
    const alertPayload: AlertPayload = {
      finding_id,
      project_id,
      project_name: findingDetail?.project_name ?? null,
      rule_change_id: null,
      rule_code_section: findingDetail?.rule_code_section ?? null,
      rule_jurisdiction: findingDetail?.rule_jurisdiction ?? null,
      status: (findingDetail?.status as AlertPayload["status"]) || "flagged",
      confidence: findingDetail?.confidence ?? 0,
      explanation: findingDetail?.explanation || "",
      cited_rule_text: findingDetail?.cited_rule_text ?? null,
      source_url: findingDetail?.source_url ?? null,
      matched_attribute: findingDetail?.matched_attribute ?? null,
      detected_at: new Date().toISOString(),
    };
    publish(alertPayload);

    return { finding_id, project_id, inbox: inboxResp, bus: "published" };
  },
);

export const functions = [
  pollRegulatorySources,
  onRegulatoryChange,
  onDesignUpdated,
  flagRaised,
];
