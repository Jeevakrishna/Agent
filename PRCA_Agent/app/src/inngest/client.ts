import { Inngest } from "inngest";

/**
 * Shared Inngest client for the PRCA Next.js app.
 *
 * Local dev uses `npx inngest-cli@latest dev` (no cloud account needed).
 * The signing key / event key are optional locally — the dev server accepts
 * all unsigned requests.
 */
export const inngest = new Inngest({
  id: "prca-nextjs-app",
  eventKey: process.env.INNGEST_EVENT_KEY || undefined,
  signingKey: process.env.INNGEST_SIGNING_KEY || undefined,
});

/** Canonical event names — single source of truth used across functions + UI. */
export const EVENTS = {
  REGULATORY_POLL_DAILY: "regulatory.poll.daily",
  REGULATORY_CHANGE_DETECTED: "regulatory.change.detected",
  DESIGN_UPDATED: "design.updated",
  COMPLIANCE_FLAG_RAISED: "compliance.flag.raised",
} as const;

export type PrcaEventName = (typeof EVENTS)[keyof typeof EVENTS];
