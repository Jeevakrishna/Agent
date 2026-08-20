import { NextRequest, NextResponse } from "next/server";
import { subscribe, getLastAlert } from "@/lib/alertBus";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const sessionId = req.cookies.get("prca_session_id")?.value || "anonymous";

  const stream = new ReadableStream({
    start(controller) {
      // Send initial hello
      const hello = `data: ${JSON.stringify({ type: "hello", session_id: sessionId })}\n\n`;
      controller.enqueue(new TextEncoder().encode(hello));

      // Send last alert if any (so new connections don't miss recent alerts)
      const last = getLastAlert();
      if (last) {
        const lastMsg = `data: ${JSON.stringify({ type: "alert", payload: last })}\n\n`;
        controller.enqueue(new TextEncoder().encode(lastMsg));
      }

      const listener = (alert: typeof last) => {
        const msg = `data: ${JSON.stringify({ type: "alert", payload: alert })}\n\n`;
        try {
          controller.enqueue(new TextEncoder().encode(msg));
        } catch {
          /* stream closed */
        }
      };

      subscribe(listener);

      // Keep-alive ping every 15s
      const ping = setInterval(() => {
        try {
          controller.enqueue(new TextEncoder().encode(": ping\n\n"));
        } catch {
          clearInterval(ping);
        }
      }, 15000);

      // Cleanup on close
      req.signal.addEventListener("abort", () => {
        clearInterval(ping);
        // Note: we don't unsubscribe here because the listener is shared
        // across all connections. In production with Redis, each connection
        // would have its own subscription channel.
      });
    },
  });

  return new NextResponse(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}
