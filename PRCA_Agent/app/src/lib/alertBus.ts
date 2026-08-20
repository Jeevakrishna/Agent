/**
 * In-memory alert bus for Step 8 realtime push.
 *
 * Production would swap this for Redis pub/sub or Inngest realtime.
 * Isolated here so the swap is a single-file change.
 */

export interface AlertPayload {
  finding_id: string;
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
  detected_at: string;
}

type Listener = (alert: AlertPayload) => void;

const listeners = new Set<Listener>();
let lastAlert: AlertPayload | null = null;

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function publish(alert: AlertPayload): void {
  lastAlert = alert;
  for (const fn of listeners) {
    try {
      fn(alert);
    } catch {
      /* ignore dead listeners */
    }
  }
}

export function getLastAlert(): AlertPayload | null {
  return lastAlert;
}
