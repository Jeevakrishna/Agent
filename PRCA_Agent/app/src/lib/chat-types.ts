/**
 * Shared chat types — single source of truth for Next.js route handlers + UI.
 *
 * All chat responses are STRUCTURED JSON. The UI renders from these fields;
 * it never parses free-form text. `type` discriminates which renderer to use.
 */

export type ChatRole = "user" | "assistant";

export type FindingStatus = "compliant" | "flagged" | "needs_review";

export interface FindingCard {
  id?: string | null;
  project_id: string;
  project_name?: string | null;
  rule_change_id?: string | null;
  rule_code_section?: string | null;
  rule_jurisdiction?: string | null;
  status: FindingStatus;
  confidence: number;
  explanation: string;
  cited_rule_text?: string | null;
  old_rule_text?: string | null;
  source_url?: string | null;
  matched_attribute?: string | null;
  suggested_action?: string | null;
  disclaimer?: string | null;
}

export interface SourceCitation {
  title: string;
  url?: string | null;
  snippet?: string | null;
}

export interface AnswerPayload {
  text: string;
  sources: SourceCitation[];
  disclaimer?: string | null;
}

export interface DiffPayload {
  rule_change_id: string;
  rule_id?: string | null;
  code_section?: string | null;
  jurisdiction_name?: string | null;
  change_type: "new" | "amended" | "repealed" | "clarification";
  old_text?: string | null;
  new_text: string;
  effective_date?: string | null;
  source_url?: string | null;
}

export interface AckPayload {
  message: string;
  /** Used by /watch: a list of jurisdictions the session is now watching. */
  watching?: string[];
}

export interface ErrorPayload {
  message: string;
  hint?: string | null;
  available_commands?: string[] | null;
}

export type ChatResponseType = "findings" | "answer" | "diff" | "ack" | "error";

export interface ChatResponseEnvelope {
  type: ChatResponseType;
  payload:
    | { findings: FindingCard[] }
    | AnswerPayload
    | DiffPayload
    | AckPayload
    | ErrorPayload;
  /** Optional — useful for timeline / debugging in the UI. */
  latency_ms?: number | null;
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  raw_text?: string | null;
  response?: ChatResponseEnvelope | null;
  created_at: string;
}
