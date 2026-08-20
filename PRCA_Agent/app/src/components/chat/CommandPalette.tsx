"use client";

import { useMemo } from "react";

const COMMANDS = [
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

interface CommandPaletteProps {
  input: string;
  index: number;
  onSelect: (cmd: string) => void;
}

export function CommandPalette({ input, index, onSelect }: CommandPaletteProps) {
  const filtered = useMemo(() => {
    const q = input.slice(1).toLowerCase();
    if (!q) return COMMANDS;
    return COMMANDS.filter(
      (c) =>
        c.token.toLowerCase().includes(q) ||
        c.description.toLowerCase().includes(q),
    );
  }, [input]);

  if (filtered.length === 0) return null;

  return (
    <div className="absolute bottom-full left-0 right-0 mb-1 border border-border bg-background shadow-none z-50">
      {filtered.map((cmd, i) => (
        <button
          key={cmd.token}
          type="button"
          onClick={() => onSelect(cmd.token)}
          className={`w-full text-left px-4 py-3 flex items-center gap-4 text-sm transition-colors ${
            i === index
              ? "bg-foreground text-background"
              : "hover:bg-foreground/5"
          }`}
        >
          <code className="font-mono text-xs tracking-tight uppercase">
            {cmd.token}
          </code>
          <span className="text-muted text-xs hidden sm:inline">
            {cmd.description}
          </span>
        </button>
      ))}
      <div className="px-4 py-2 text-[10px] text-muted border-t border-border font-mono tracking-wider uppercase">
        <span className="mr-3">↑↓ Navigate</span>
        <span className="mr-3">↵ Select</span>
        <span>Esc Close</span>
      </div>
    </div>
  );
}
