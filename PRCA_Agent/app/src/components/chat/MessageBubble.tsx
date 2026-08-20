"use client";

import type { ChatMessage } from "@/lib/chat-types";
import { FindingCard } from "./FindingCard";
import { DiffView } from "./DiffView";
import { SourcesList } from "./SourcesList";

interface MessageBubbleProps {
  message: ChatMessage;
}

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user";
  const response = message.response;

  return (
    <div
      className={`flex ${isUser ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`max-w-[85%] md:max-w-[70%] ${
          isUser
            ? "bg-foreground text-background"
            : "bg-transparent border border-border"
        }`}
      >
        {/* User message */}
        {isUser && (
          <div className="px-4 py-3">
            <p className="text-sm leading-relaxed whitespace-pre-wrap">
              {message.raw_text}
            </p>
          </div>
        )}

        {/* Assistant message with structured response */}
        {!isUser && response && (
          <div className="border-b border-border last:border-b-0">
            {/* Raw text echo */}
            {message.raw_text && (
              <div className="px-4 py-2 border-b border-border bg-foreground/5">
                <p className="text-xs text-muted font-mono tracking-wider uppercase">
                  {message.raw_text}
                </p>
              </div>
            )}

            {/* Render by type */}
            {response.type === "findings" && (
              <div className="divide-y divide-border">
                {((response.payload as { findings: unknown[] }).findings || []).map(
                  (f: unknown) => (
                    <div key={(f as { id?: string | null }).id || (f as { project_id: string }).project_id} className="p-4">
                      <FindingCard finding={f as Parameters<typeof FindingCard>[0]["finding"]} />
                    </div>
                  ),
                )}
              </div>
            )}

            {response.type === "answer" && (
              <div className="p-4 space-y-3">
                <p className="text-sm leading-relaxed whitespace-pre-wrap">
                  {(response.payload as { text: string }).text}
                </p>
                <SourcesList
                  sources={(response.payload as { sources?: { title: string; url?: string | null; snippet?: string | null }[] }).sources || []}
                />
              </div>
            )}

            {response.type === "diff" && (
              <div className="p-4">
                <DiffView payload={response.payload as Parameters<typeof DiffView>[0]["payload"]} />
              </div>
            )}

            {response.type === "ack" && (
              <div className="p-4 space-y-3">
                <p className="text-sm">
                  {(response.payload as { message: string }).message}
                </p>
                {(response.payload as { watching?: string[] }).watching &&
                  (response.payload as { watching: string[] }).watching.length > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {(response.payload as { watching: string[] }).watching.map((j: string) => (
                        <span
                          key={j}
                          className="inline-flex items-center border border-border px-2 py-0.5 text-xs font-mono uppercase tracking-wider"
                        >
                          {j}
                        </span>
                      ))}
                    </div>
                  )}
              </div>
            )}

            {response.type === "error" && (
              <div className="p-4 space-y-2 border-l-2 border-foreground">
                <p className="text-sm font-semibold uppercase tracking-tight">
                  {(response.payload as { message: string }).message}
                </p>
                {(response.payload as { hint?: string | null }).hint && (
                  <p className="text-xs text-muted">
                    {(response.payload as { hint: string }).hint}
                  </p>
                )}
                {(response.payload as { available_commands?: string[] | null }).available_commands && (
                  <div className="mt-3 space-y-1">
                    {(response.payload as { available_commands: string[] }).available_commands.map((cmd: string) => (
                      <p
                        key={cmd}
                        className="text-xs text-muted font-mono"
                      >
                        {cmd}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Latency */}
            {response.latency_ms != null && (
              <div className="px-4 py-2 border-t border-border">
                <p className="text-[10px] text-muted font-mono tracking-wider uppercase">
                  {response.latency_ms}ms
                </p>
              </div>
            )}
          </div>
        )}

        {/* Timestamp */}
        <div className={`px-4 py-2 ${isUser ? "bg-foreground/10" : "border-t border-border"}`}>
          <p className="text-[10px] text-muted font-mono tracking-wider uppercase">
            {formatTime(message.created_at)}
          </p>
        </div>
      </div>
    </div>
  );
}
