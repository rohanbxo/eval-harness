/**
 * Read-only mode for the public demo.
 *
 * The deployed dashboard serves a frozen snapshot of one four-model run and the
 * API holds no model credentials, so every control that would launch or cancel a
 * run is hidden rather than left to fail. The API refuses those routes with 405
 * independently (`EVALHARNESS_READ_ONLY`); this flag is only about not offering
 * a button that cannot work.
 *
 * `NEXT_PUBLIC_READ_ONLY` is inlined at build time, so it must be present as a
 * build argument, not just at runtime. See deploy/web.Dockerfile.
 */
export const READ_ONLY = process.env.NEXT_PUBLIC_READ_ONLY === "true";

/** Where the demo's explanatory note points for the full story. */
export const WRITEUP_URL =
  "https://github.com/rohanbxo/eval-harness/blob/main/docs/WRITEUP.md";

/**
 * The finding the demo exists to show: gpt-oss-120b-groq complying with the
 * research-injection prompt injection, run a40a660a, repetition 1.
 */
export const FEATURED_ATTEMPT_ID = "169884a52a144df79e4911c4fa004038";

/** One sentence of context, not just "this is disabled". */
export const READ_ONLY_NOTE =
  "This is a static snapshot of one four-model run — the dashboard is live, but launching runs is disabled and the deployment holds no model API keys.";
