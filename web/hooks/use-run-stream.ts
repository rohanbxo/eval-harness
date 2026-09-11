"use client";

import { useEffect, useRef, useState } from "react";

import { runStreamUrl } from "@/lib/client-api";
import {
  ATTEMPT_STATUSES,
  RUN_STATUSES,
  type AttemptStatus,
  type ProgressEventType,
  type RunStatus,
  type RunStreamEvent,
} from "@/lib/types";

export type StreamState = "connecting" | "open" | "closed" | "error";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Every event name the backend's ProgressEvent can carry (SPEC 9.1). */
const PROGRESS_EVENT_TYPES = [
  "snapshot",
  "run_started",
  "attempt_started",
  "attempt_finished",
  "run_completed",
  "heartbeat",
] as const;

function asProgressEventType(value: unknown): ProgressEventType | null {
  return typeof value === "string" && (PROGRESS_EVENT_TYPES as readonly string[]).includes(value)
    ? (value as ProgressEventType)
    : null;
}

function asAttemptStatus(value: unknown): AttemptStatus | null {
  return typeof value === "string" && (ATTEMPT_STATUSES as readonly string[]).includes(value)
    ? (value as AttemptStatus)
    : null;
}

function asRunStatus(value: unknown): RunStatus | null {
  return typeof value === "string" && (RUN_STATUSES as readonly string[]).includes(value)
    ? (value as RunStatus)
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * Parses one SSE payload into a `ProgressEvent`. The stream may either name its
 * events (`event: attempt_started`) or carry the discriminator in the JSON
 * body; both are accepted. Anything unrecognised is ignored rather than thrown,
 * because a dropped progress frame should never break the page — the run detail
 * query is the source of truth and will catch up.
 */
export function parseStreamEvent(raw: string, eventName?: string): RunStreamEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(parsed)) {
    return null;
  }

  const type = asProgressEventType(parsed.type) ?? asProgressEventType(eventName);
  const runId = asString(parsed.run_id);
  if (type === null || runId === null) {
    return null;
  }

  return {
    type,
    run_id: runId,
    ts: asString(parsed.ts) ?? new Date().toISOString(),
    run_status: asRunStatus(parsed.run_status),
    attempt_id: asString(parsed.attempt_id),
    scenario_id: asString(parsed.scenario_id),
    repetition: typeof parsed.repetition === "number" ? parsed.repetition : null,
    attempt_status: asAttemptStatus(parsed.attempt_status),
    passed: typeof parsed.passed === "boolean" ? parsed.passed : null,
    completed_attempts:
      typeof parsed.completed_attempts === "number" ? parsed.completed_attempts : null,
    total_attempts: typeof parsed.total_attempts === "number" ? parsed.total_attempts : null,
    message: asString(parsed.message),
  };
}

export interface UseRunStreamOptions {
  runId: string;
  /** Stop listening once the run is in a terminal state. */
  enabled: boolean;
  onEvent: (event: RunStreamEvent) => void;
}

/**
 * Subscribes to `GET /api/runs/{id}/stream`. Nothing is persisted client-side:
 * events only nudge the UI, and the database stays the source of truth.
 */
export function useRunStream({ runId, enabled, onEvent }: UseRunStreamOptions): StreamState {
  const [state, setState] = useState<StreamState>(enabled ? "connecting" : "closed");
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    if (!enabled || typeof window === "undefined" || typeof EventSource === "undefined") {
      setState("closed");
      return;
    }

    setState("connecting");
    const source = new EventSource(runStreamUrl(runId));

    const dispatch = (raw: string, name?: string) => {
      const parsed = parseStreamEvent(raw, name);
      if (parsed) {
        handlerRef.current(parsed);
      }
    };

    source.onopen = () => setState("open");
    source.onmessage = (message: MessageEvent<string>) => dispatch(message.data);
    source.onerror = () => {
      // EventSource retries on its own; surface the state so the UI can show
      // that live updates are degraded and fall back to polling.
      setState("error");
    };

    const named = PROGRESS_EVENT_TYPES;
    const listeners = named.map((name) => {
      const listener = (event: Event) => {
        if (event instanceof MessageEvent && typeof event.data === "string") {
          dispatch(event.data, name);
        }
      };
      source.addEventListener(name, listener);
      return { name, listener };
    });

    return () => {
      for (const { name, listener } of listeners) {
        source.removeEventListener(name, listener);
      }
      source.close();
      setState("closed");
    };
  }, [runId, enabled]);

  return state;
}
