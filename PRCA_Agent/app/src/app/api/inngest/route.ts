import { serve } from "inngest/next";
import { inngest } from "@/inngest/client";
import { functions } from "@/inngest/functions";

/**
 * Standard Inngest serve handler for the Next.js App Router.
 *
 * - Registers all four PRCA functions with the dev server / Inngest cloud.
 * - Local: `npx inngest-cli@latest dev` connects here to sync function defs.
 * - Path: POST/GET /api/inngest
 */
export const { GET, POST, PUT } = serve({
  client: inngest,
  functions,
});
