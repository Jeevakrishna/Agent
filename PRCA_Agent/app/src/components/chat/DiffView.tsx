"use client";

import type { DiffPayload } from "@/lib/chat-types";

const CHANGE_TYPE_LABEL: Record<DiffPayload["change_type"], string> = {
  new: "NEW",
  amended: "AMENDED",
  repealed: "REPEALED",
  clarification: "CLARIFICATION",
};

interface DiffViewProps {
  payload: DiffPayload;
}

export function DiffView({ payload }: DiffViewProps) {
  return (
    <div className="border border-border">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono tracking-widest uppercase text-muted">
            {CHANGE_TYPE_LABEL[payload.change_type]}
          </span>
          {payload.code_section && (
            <code className="text-xs font-mono uppercase tracking-tight">
              {payload.code_section}
            </code>
          )}
        </div>
        {payload.effective_date && (
          <span className="text-xs font-mono text-muted">
            {new Date(payload.effective_date).toLocaleDateString()}
          </span>
        )}
      </div>

      {/* Two-column diff */}
      <div className="grid grid-cols-1 md:grid-cols-2 divide-y md:divide-y-0 md:divide-x divide-border">
        {/* Old text */}
        <div className="p-4">
          <div className="text-xs font-mono tracking-widest uppercase text-muted mb-3">
            Before
          </div>
          <div className="text-sm leading-relaxed whitespace-pre-wrap max-h-64 overflow-y-auto">
            {payload.old_text || (
              <span className="text-muted italic">(no prior version)</span>
            )}
          </div>
        </div>

        {/* New text */}
        <div className="p-4">
          <div className="text-xs font-mono tracking-widest uppercase text-muted mb-3">
            After
          </div>
          <div className="text-sm leading-relaxed whitespace-pre-wrap max-h-64 overflow-y-auto">
            {payload.new_text}
          </div>
        </div>
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between border-t border-border px-4 py-2 bg-foreground/5">
        <span className="text-[10px] font-mono text-muted uppercase tracking-wider">
          {payload.rule_change_id?.slice(0, 8)}...
        </span>
        {payload.source_url && (
          <a
            href={payload.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs font-mono underline underline-offset-2"
          >
            Source
          </a>
        )}
      </div>
    </div>
  );
}
