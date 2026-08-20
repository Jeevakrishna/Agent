"use client";

import { useState } from "react";
import type { SourceCitation } from "@/lib/chat-types";

interface SourcesListProps {
  sources: SourceCitation[];
}

export function SourcesList({ sources }: SourcesListProps) {
  const [open, setOpen] = useState(false);

  if (!sources || sources.length === 0) return null;

  return (
    <div className="mt-4">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-xs font-mono tracking-wider uppercase text-muted hover:text-foreground transition-colors"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          className={`w-3.5 h-3.5 transition-transform ${open ? "rotate-90" : ""}`}
        >
          <path d="M9 18l6-6-6-6" />
        </svg>
        Sources ({sources.length})
      </button>
      {open && (
        <ul className="mt-3 space-y-3 pl-5">
          {sources.map((src, i) => (
            <li key={i} className="text-sm">
              {src.url ? (
                <a
                  href={src.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-mono text-xs underline underline-offset-2 hover:no-underline"
                >
                  {src.title || "(untitled)"}
                </a>
              ) : (
                <span className="font-mono text-xs">{src.title || "(untitled)"}</span>
              )}
              {src.snippet && (
                <p className="text-muted text-xs mt-1 leading-relaxed line-clamp-2">
                  {src.snippet}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
