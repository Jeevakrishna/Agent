"use client";

import type { FindingCard } from "@/lib/chat-types";

const STATUS_CONFIG: Record<
  FindingCard["status"],
  { label: string; border: string; bg: string; text: string }
> = {
  compliant: {
    label: "COMPLIANT",
    border: "border-foreground",
    bg: "bg-background",
    text: "text-foreground",
  },
  flagged: {
    label: "FLAGGED",
    border: "border-foreground",
    bg: "bg-foreground text-background",
    text: "text-background",
  },
  needs_review: {
    label: "NEEDS REVIEW",
    border: "border-foreground",
    bg: "bg-background",
    text: "text-foreground",
  },
};

interface FindingCardProps {
  finding: FindingCard;
}

export function FindingCard({ finding }: FindingCardProps) {
  const statusConf = STATUS_CONFIG[finding.status];

  return (
    <div className={`border border-border ${statusConf.bg} ${statusConf.text}`}>
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex items-center gap-3">
          <span
            className={`text-xs font-mono tracking-widest uppercase ${statusConf.text}`}
          >
            {statusConf.label}
          </span>
          {finding.project_name && (
            <span className="text-sm font-semibold uppercase tracking-tight">
              {finding.project_name}
            </span>
          )}
        </div>
        <span className="text-xs font-mono text-muted">
          {Math.round((finding.confidence || 0) * 100)}%
        </span>
      </div>

      {/* Rule info */}
      {finding.rule_code_section && (
        <div className="px-4 py-2 border-b border-border">
          <span className="text-xs font-mono text-muted uppercase tracking-wider">
            {finding.rule_code_section}
          </span>
          {finding.rule_jurisdiction && (
            <span className="text-xs font-mono text-muted ml-2">
              {finding.rule_jurisdiction}
            </span>
          )}
        </div>
      )}

      {/* Explanation */}
      <div className="px-4 py-3 text-sm leading-relaxed">
        {finding.explanation}
      </div>

      {/* Suggested action for flagged */}
      {finding.status === "flagged" && finding.suggested_action && (
        <div className="border-t border-border px-4 py-3 bg-foreground/5">
          <p className="text-xs font-mono text-muted uppercase tracking-wider mb-1">
            Action
          </p>
          <p className="text-sm">{finding.suggested_action}</p>
        </div>
      )}

      {/* Conflicting attribute */}
      {finding.matched_attribute && (
        <div className="border-t border-border px-4 py-2 text-xs font-mono text-muted uppercase tracking-wider">
          Attribute: {finding.matched_attribute}
        </div>
      )}

      {/* Source link */}
      {finding.source_url && (
        <div className="border-t border-border px-4 py-2">
          <a
            href={finding.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs font-mono underline underline-offset-2 hover:no-underline"
          >
            View source
          </a>
        </div>
      )}

      {/* Disclaimer */}
      {finding.disclaimer && (
        <div className="border-t border-border px-4 py-2 text-xs text-muted italic">
          {finding.disclaimer}
        </div>
      )}
    </div>
  );
}
