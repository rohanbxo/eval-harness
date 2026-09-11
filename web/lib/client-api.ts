"use client";

/**
 * Browser-side API client, used by the interactive pieces (live run view,
 * launcher, compare picker). These throw on failure; TanStack Query turns that
 * into an error state.
 */

import type {
  AttemptTrace,
  CreateRunRequest,
  ModelEntry,
  Run,
  RunDetail,
  RunListPage,
  RunStatus,
  ScenarioListItem,
} from "@/lib/types";

export function apiBase(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
}

export class ApiRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      ...init,
      headers: {
        accept: "application/json",
        ...(init?.body ? { "content-type": "application/json" } : {}),
        ...init?.headers,
      },
      cache: "no-store",
    });
  } catch (error) {
    throw new ApiRequestError(
      error instanceof Error ? `Could not reach the API at ${apiBase()}: ${error.message}` : "Could not reach the API",
      0,
    );
  }

  if (!response.ok) {
    let detail = response.statusText || `HTTP ${response.status}`;
    try {
      const text = await response.text();
      if (text) {
        try {
          const parsed: unknown = JSON.parse(text);
          if (parsed && typeof parsed === "object" && "detail" in parsed) {
            const value = (parsed as { detail: unknown }).detail;
            detail = typeof value === "string" ? value : JSON.stringify(value);
          } else {
            detail = text.slice(0, 400);
          }
        } catch {
          detail = text.slice(0, 400);
        }
      }
    } catch {
      // keep the status text
    }
    throw new ApiRequestError(detail, response.status);
  }

  return (await response.json()) as T;
}

export function fetchModels(): Promise<ModelEntry[]> {
  return request<ModelEntry[]>("/api/models");
}

export function fetchScenarios(): Promise<ScenarioListItem[]> {
  return request<ScenarioListItem[]>("/api/scenarios");
}

export function fetchRun(id: string): Promise<RunDetail> {
  return request<RunDetail>(`/api/runs/${encodeURIComponent(id)}`);
}

export function fetchRuns(params: { model?: string; status?: RunStatus; page?: number } = {}): Promise<RunListPage> {
  const query = new URLSearchParams();
  if (params.model) query.set("model", params.model);
  if (params.status) query.set("status", params.status);
  if (params.page !== undefined) query.set("page", String(params.page));
  const queryString = query.toString();
  return request<RunListPage>(`/api/runs${queryString ? `?${queryString}` : ""}`);
}

export function fetchAttempt(id: string): Promise<AttemptTrace> {
  return request<AttemptTrace>(`/api/attempts/${encodeURIComponent(id)}`);
}

export function postRun(body: CreateRunRequest): Promise<Run> {
  return request<Run>("/api/runs", { method: "POST", body: JSON.stringify(body) });
}

export function cancelRun(id: string): Promise<Run> {
  return request<Run>(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export function runStreamUrl(id: string): string {
  return `${apiBase()}/api/runs/${encodeURIComponent(id)}/stream`;
}
