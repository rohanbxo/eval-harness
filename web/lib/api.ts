/**
 * Server-side API client.
 *
 * Every read returns an `ApiResult` rather than throwing, so a page renders an
 * error state instead of crashing when the API is down. Nothing here is ever
 * called at build time: every page that fetches is dynamically rendered.
 */

import type {
  AttemptTrace,
  CompareResponse,
  CreateRunRequest,
  HealthResponse,
  LeaderboardResponse,
  ModelEntry,
  ModelListResponse,
  Run,
  RunDetail,
  RunListPage,
  RunStatus,
  ScenarioDetail,
  ScenarioListItem,
  ScenarioListResponse,
} from "@/lib/types";

export type ApiResult<T> = { ok: true; data: T } | { ok: false; error: ApiError };

export interface ApiError {
  /** HTTP status, or 0 when the request never reached the API. */
  status: number;
  message: string;
  /** True when the API could not be contacted at all. */
  unreachable: boolean;
}

/** Base URL for server components: the compose service name inside Docker. */
export function serverApiBase(): string {
  return process.env.API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
}

/** Base URL for the browser. */
export function browserApiBase(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
}

function describe(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

async function readErrorBody(response: Response): Promise<string> {
  try {
    const text = await response.text();
    if (!text) {
      return response.statusText || `HTTP ${response.status}`;
    }
    try {
      const parsed: unknown = JSON.parse(text);
      if (parsed && typeof parsed === "object" && "detail" in parsed) {
        const detail = (parsed as { detail: unknown }).detail;
        if (typeof detail === "string") {
          return detail;
        }
        return JSON.stringify(detail);
      }
    } catch {
      // not JSON; fall through to the raw text
    }
    return text.slice(0, 500);
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  /** Seconds; the request is aborted after this. */
  timeoutSeconds?: number;
}

/**
 * Fetch JSON from the API. Never throws: network failures, non-2xx responses
 * and malformed JSON all come back as `{ ok: false }`.
 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
  const { method = "GET", body, timeoutSeconds = 15 } = options;
  const url = `${serverApiBase()}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSeconds * 1000);

  try {
    const response = await fetch(url, {
      method,
      headers: body === undefined ? { accept: "application/json" } : { accept: "application/json", "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: controller.signal,
    });

    if (!response.ok) {
      return { ok: false, error: { status: response.status, message: await readErrorBody(response), unreachable: false } };
    }

    const data = (await response.json()) as T;
    return { ok: true, data };
  } catch (error) {
    return {
      ok: false,
      error: { status: 0, message: describe(error), unreachable: true },
    };
  } finally {
    clearTimeout(timer);
  }
}

/* -------------------------------------------------------------------------- */
/* Typed endpoints (SPEC 9.2)                                                  */
/* -------------------------------------------------------------------------- */

export function getHealth(): Promise<ApiResult<HealthResponse>> {
  return apiFetch<HealthResponse>("/api/health", { timeoutSeconds: 5 });
}

export async function getScenarios(): Promise<ApiResult<ScenarioListItem[]>> {
  const result = await apiFetch<ScenarioListResponse>("/api/scenarios");
  return result.ok ? { ok: true, data: result.data.scenarios } : result;
}

export function getScenario(id: string): Promise<ApiResult<ScenarioDetail>> {
  return apiFetch<ScenarioDetail>(`/api/scenarios/${encodeURIComponent(id)}`);
}

export async function getModels(): Promise<ApiResult<ModelEntry[]>> {
  const result = await apiFetch<ModelListResponse>("/api/models");
  return result.ok ? { ok: true, data: result.data.models } : result;
}

export function createRun(request: CreateRunRequest): Promise<ApiResult<Run>> {
  return apiFetch<Run>("/api/runs", { method: "POST", body: request, timeoutSeconds: 30 });
}

/** Runs per page. The API paginates by limit/offset; the UI thinks in pages. */
export const RUNS_PAGE_SIZE = 25;

export function getRuns(
  params: { model?: string; status?: RunStatus; page?: number } = {},
): Promise<ApiResult<RunListPage>> {
  const page = Math.max(1, params.page ?? 1);
  const query = new URLSearchParams();
  if (params.model) query.set("model_key", params.model);
  if (params.status) query.set("status", params.status);
  query.set("limit", String(RUNS_PAGE_SIZE));
  query.set("offset", String((page - 1) * RUNS_PAGE_SIZE));
  return apiFetch<RunListPage>(`/api/runs?${query.toString()}`);
}

export function getRun(id: string): Promise<ApiResult<RunDetail>> {
  return apiFetch<RunDetail>(`/api/runs/${encodeURIComponent(id)}`);
}

export function getAttempt(id: string): Promise<ApiResult<AttemptTrace>> {
  return apiFetch<AttemptTrace>(`/api/attempts/${encodeURIComponent(id)}`, { timeoutSeconds: 30 });
}

export function getLeaderboard(): Promise<ApiResult<LeaderboardResponse>> {
  return apiFetch<LeaderboardResponse>("/api/leaderboard");
}

export function getCompare(runA: string, runB: string): Promise<ApiResult<CompareResponse>> {
  const query = new URLSearchParams({ run_a: runA, run_b: runB });
  return apiFetch<CompareResponse>(`/api/compare?${query.toString()}`);
}
