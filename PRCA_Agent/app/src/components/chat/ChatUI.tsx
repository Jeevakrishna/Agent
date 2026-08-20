"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import type { ChatMessage, ChatResponseEnvelope, FindingCard } from "@/lib/chat-types";
import { CommandPalette } from "./CommandPalette";
import { MessageBubble } from "./MessageBubble";
import { WatchChip } from "./WatchChip";

const AGENT_API_URL =
  process.env.NEXT_PUBLIC_AGENT_API_URL || "http://localhost:8000";

export default function ChatUI() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [showPalette, setShowPalette] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const [watching, setWatching] = useState<string[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);

  // Load watch status on mount
  useEffect(() => {
    fetch("/api/watch")
      .then((r) => r.json())
      .then((data) => {
        if (data.ok) {
          setWatching(data.watching || []);
          setSessionId(data.session_id);
        }
      })
      .catch(() => {});
  }, []);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Check if user is scrolled up
  const isScrolledUp = useCallback(() => {
    const el = messagesContainerRef.current;
    if (!el) return false;
    return el.scrollHeight - el.scrollTop - el.clientHeight > 150;
  }, []);

  // Append alert as a system message
  const appendAlert = useCallback(
    (alert: {
      finding_id: string;
      project_id: string;
      project_name?: string | null;
      rule_code_section?: string | null;
      rule_jurisdiction?: string | null;
      status: string;
      confidence: number;
      explanation: string;
      cited_rule_text?: string | null;
      source_url?: string | null;
      matched_attribute?: string | null;
      detected_at: string;
    }) => {
      const scrolledUp = isScrolledUp();

      const findingCard: FindingCard = {
        id: alert.finding_id,
        project_id: alert.project_id,
        project_name: alert.project_name ?? undefined,
        rule_change_id: undefined,
        rule_code_section: alert.rule_code_section ?? undefined,
        rule_jurisdiction: alert.rule_jurisdiction ?? undefined,
        status: alert.status as FindingCard["status"],
        confidence: alert.confidence,
        explanation: alert.explanation,
        cited_rule_text: alert.cited_rule_text ?? undefined,
        source_url: alert.source_url ?? undefined,
        matched_attribute: alert.matched_attribute ?? undefined,
        suggested_action:
          alert.status === "flagged"
            ? "Revise design attribute and re-file permit."
            : null,
        disclaimer: "Flagged for review — not a legal compliance determination.",
      };

      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        raw_text: `New regulatory alert: ${alert.project_name || alert.project_id}`,
        response: {
          type: "findings",
          payload: { findings: [findingCard] },
          latency_ms: 0,
        },
        created_at: new Date().toISOString(),
      };

      setMessages((prev) => [...prev, assistantMsg]);

      if (scrolledUp) {
        setToast("New regulatory alert");
        setTimeout(() => setToast(null), 4000);
      }
    },
    [isScrolledUp],
  );

  // SSE subscription with polling fallback
  useEffect(() => {
    let sseAttempts = 0;
    const MAX_SSE_ATTEMPTS = 2;
    let pollInterval: ReturnType<typeof setInterval> | null = null;

    const connectSSE = () => {
      const eventSource = new EventSource("/api/alerts/stream");

      eventSource.onopen = () => {
        sseAttempts = 0;
        if (pollInterval) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
        console.log("[chat] SSE connected");
      };

      eventSource.onerror = () => {
        eventSource.close();
        sseAttempts += 1;
        console.warn(
          `[chat] SSE failed (attempt ${sseAttempts}/${MAX_SSE_ATTEMPTS})`,
        );

        if (sseAttempts >= MAX_SSE_ATTEMPTS && !pollInterval) {
          console.warn("[chat] Falling back to polling /api/alerts/inbox");
          pollInterval = setInterval(() => {
            fetch("/api/alerts/inbox")
              .then((r) => r.json())
              .then((data) => {
                if (data.ok && data.alerts && data.alerts.length > 0) {
                  const latest = data.alerts[0];
                  setMessages((prev) => {
                    const already = prev.some(
                      (m) =>
                        m.response?.type === "findings" &&
                        ((m.response.payload as { findings: FindingCard[] }).findings || []).some(
                          (f: FindingCard) => f.id === latest.finding_id,
                        ),
                    );
                    if (already) return prev;
                    fetch(`${AGENT_API_URL}/findings`, {})
                      .then((r) => r.json())
                      .then((findings: FindingCard[]) => {
                        const detail = findings.find(
                          (f) => f.id === latest.finding_id,
                        );
                        if (detail) {
                          appendAlert({
                            finding_id: detail.id || latest.finding_id,
                            project_id: detail.project_id,
                            project_name: detail.project_name,
                            rule_code_section: detail.rule_code_section,
                            rule_jurisdiction: detail.rule_jurisdiction,
                            status: detail.status,
                            confidence: detail.confidence,
                            explanation: detail.explanation,
                            cited_rule_text: detail.cited_rule_text,
                            source_url: detail.source_url,
                            matched_attribute: detail.matched_attribute,
                            detected_at: latest.raised_at,
                          });
                        }
                      })
                      .catch(() => {});
                    return prev;
                  });
                }
              })
              .catch(() => {});
          }, 5000);
        }
      };

      eventSource.addEventListener("alert", ((e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          if (data.type === "alert" && data.payload) {
            appendAlert(data.payload);
          }
        } catch {
          /* ignore malformed */
        }
      }) as EventListener);

      return eventSource;
    };

    const es = connectSSE();

    return () => {
      es.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [appendAlert, AGENT_API_URL]);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim() || loading) return;

      const userMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        raw_text: text.trim(),
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userMsg]);
      setInput("");
      setShowPalette(false);
      setPaletteIndex(0);
      setLoading(true);

      try {
        const res = await fetch("/api/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text.trim() }),
        });
        const envelope: ChatResponseEnvelope = await res.json();

        const assistantMsg: ChatMessage = {
          id: crypto.randomUUID(),
          role: "assistant",
          raw_text: text.trim(),
          response: envelope,
          created_at: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, assistantMsg]);

        if (envelope.type === "ack") {
          const ack = envelope.payload as { watching?: string[] };
          if (ack.watching) {
            setWatching(ack.watching);
          }
        }
      } catch (err) {
        const errorMsg: ChatMessage = {
          id: crypto.randomUUID(),
          role: "assistant",
          raw_text: text.trim(),
          response: {
            type: "error",
            payload: {
              message: err instanceof Error ? err.message : "Unknown error",
              hint: "Is the agent service running?",
            },
            latency_ms: 0,
          },
          created_at: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, errorMsg]);
      } finally {
        setLoading(false);
        inputRef.current?.focus();
      }
    },
    [loading],
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (showPalette) {
      const commands = ["/permit-check", "/watch", "/diff", "/ask"];
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setPaletteIndex((i) => (i + 1) % commands.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setPaletteIndex((i) => (i - 1 + commands.length) % commands.length);
        return;
      }
      if (e.key === "Tab" || e.key === "Enter") {
        e.preventDefault();
        const selected = commands[paletteIndex];
        setInput(selected + " ");
        setShowPalette(false);
        setPaletteIndex(0);
        return;
      }
      if (e.key === "Escape") {
        setShowPalette(false);
        setPaletteIndex(0);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setInput(val);
    if (val.startsWith("/")) {
      setShowPalette(true);
      setPaletteIndex(0);
    } else {
      setShowPalette(false);
    }
  };

  return (
    <div className="flex flex-col h-screen bg-background text-foreground">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-border px-6 py-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold tracking-tight uppercase">
            PRCA Agent
          </h1>
          <span className="text-xs text-muted font-mono tracking-wider uppercase">
            Compliance Chat
          </span>
        </div>
        {watching.length > 0 && <WatchChip jurisdictions={watching} />}
      </header>

      {/* Toast banner */}
      {toast && (
        <div className="bg-foreground text-background border-b border-border px-6 py-3 text-sm font-mono tracking-wide flex items-center justify-between">
          <span>{toast}</span>
          <button
            onClick={() => setToast(null)}
            className="ml-4 text-background/60 hover:text-background transition-colors"
            aria-label="Dismiss"
          >
            &times;
          </button>
        </div>
      )}

      {/* Messages */}
      <div
        ref={messagesContainerRef}
        className="flex-1 overflow-y-auto"
      >
        <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center py-24 text-center">
              <p className="text-muted text-sm max-w-md leading-relaxed">
                Type a message to ask a question, run a compliance check, or
                press <code className="font-mono text-xs border border-border px-1.5 py-0.5">/</code> for commands.
              </p>
            </div>
          )}
          {messages.map((msg) => (
            <MessageBubble key={msg.id} message={msg} />
          ))}
          {loading && (
            <div className="flex items-center gap-3 text-muted text-sm font-mono">
              <div className="w-1.5 h-1.5 bg-foreground animate-pulse" />
              <span>Processing...</span>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Input area */}
      <div className="border-t border-border px-6 py-4">
        <div className="relative max-w-3xl mx-auto">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            placeholder="Type a message..."
            className="w-full bg-transparent border border-border px-4 py-3 pr-12 text-sm text-foreground placeholder:text-muted focus:outline-none focus:border-foreground transition-colors"
            disabled={loading}
          />
          <button
            onClick={() => sendMessage(input)}
            disabled={loading || !input.trim()}
            className="absolute right-3 top-1/2 -translate-y-1/2 p-1.5 text-muted hover:text-foreground disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            aria-label="Send"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.5}
              className="w-5 h-5"
            >
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </button>

          {/* Command palette */}
          {showPalette && (
            <CommandPalette
              input={input}
              index={paletteIndex}
              onSelect={(cmd: string) => {
                setInput(cmd + " ");
                setShowPalette(false);
                setPaletteIndex(0);
                inputRef.current?.focus();
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
