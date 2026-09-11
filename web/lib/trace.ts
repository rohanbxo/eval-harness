/**
 * Turns a flat, ordered event log (SPEC 8.3) into the shape the trace viewer
 * renders: one timeline per turn, with tool calls paired to their results and
 * the assertions that referenced them attached inline.
 *
 * Event payloads are arbitrary JSON (the backend's jsonb column), so everything
 * here reads them defensively through typed accessors - no `any`, and an
 * unexpected payload degrades to "show the raw JSON" rather than crashing.
 */

import type { AssertionResult, AttemptTrace, TraceEvent, TraceTurn } from "@/lib/types";

/** An event's `payload` column as it arrives: arbitrary JSON from jsonb. */
export type EventPayload = NonNullable<TraceEvent["payload"]>;

/* -------------------------------------------------------------------------- */
/* View models                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * A `tool_call` event's payload, normalized for display. These are view models
 * rather than API types: the wire shape is arbitrary JSON, and the readers below
 * accept several key spellings so a payload tweak degrades to a missing field
 * instead of a broken page.
 */
export interface ToolCallPayloadView {
  toolCallId: string | null;
  name: string;
  arguments: EventPayload;
  rawArguments: string | null;
  /** Set when the model emitted tool-call JSON the runner could not parse. */
  parseError: string | null;
  faulted: boolean;
}

/** A `tool_result` event's payload, normalized for display. */
export interface ToolResultPayloadView {
  toolCallId: string | null;
  name: string | null;
  ok: boolean;
  content: unknown;
  error: string | null;
  faulted: boolean;
}

/* -------------------------------------------------------------------------- */
/* Safe payload accessors                                                      */
/* -------------------------------------------------------------------------- */

export function readValue(payload: EventPayload | undefined, ...keys: string[]): unknown {
  if (!payload) return undefined;
  for (const key of keys) {
    const value = payload[key];
    if (value !== undefined && value !== null) {
      return value;
    }
  }
  return undefined;
}

export function readString(payload: EventPayload | undefined, ...keys: string[]): string | null {
  const value = readValue(payload, ...keys);
  return typeof value === "string" ? value : null;
}

export function readNumber(payload: EventPayload | undefined, ...keys: string[]): number | null {
  const value = readValue(payload, ...keys);
  return typeof value === "number" ? value : null;
}

export function readBoolean(payload: EventPayload | undefined, ...keys: string[]): boolean | null {
  const value = readValue(payload, ...keys);
  return typeof value === "boolean" ? value : null;
}

export function readObject(
  payload: EventPayload | undefined,
  ...keys: string[]
): EventPayload | null {
  const value = readValue(payload, ...keys);
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as EventPayload;
  }
  return null;
}

export function readArray(payload: EventPayload | undefined, ...keys: string[]): unknown[] | null {
  const value = readValue(payload, ...keys);
  return Array.isArray(value) ? value : null;
}

/* -------------------------------------------------------------------------- */
/* Payload views                                                               */
/* -------------------------------------------------------------------------- */

export function toolCallView(event: TraceEvent): ToolCallPayloadView {
  const payload = event.payload;
  const nested = readObject(payload, "call", "tool_call");
  const source = nested ?? payload;
  return {
    toolCallId: readString(source, "id", "tool_call_id", "call_id"),
    name: readString(source, "name", "tool", "tool_name") ?? "unknown_tool",
    arguments: readObject(source, "arguments", "args") ?? {},
    rawArguments: readString(source, "raw_arguments"),
    parseError: readString(source, "parse_error"),
    faulted: readBoolean(payload, "faulted", "fault") ?? false,
  };
}

export function toolResultView(event: TraceEvent): ToolResultPayloadView {
  const payload = event.payload;
  const nested = readObject(payload, "result", "tool_result");
  const source = nested ?? payload;
  const content = readValue(source, "content", "response");
  return {
    toolCallId: readString(payload, "tool_call_id", "call_id", "id"),
    name: readString(payload, "name", "tool", "tool_name") ?? readString(source, "name", "tool"),
    ok: readBoolean(source, "ok") ?? true,
    content: content === undefined ? null : content,
    error: readString(source, "error"),
    faulted: (readBoolean(payload, "faulted", "fault") ?? false) || (readBoolean(source, "faulted") ?? false),
  };
}

export function modelResponseText(event: TraceEvent): string | null {
  const nested = readObject(event.payload, "message", "response");
  return readString(event.payload, "content", "text") ?? readString(nested ?? undefined, "content", "text");
}

/* -------------------------------------------------------------------------- */
/* Timeline model                                                              */
/* -------------------------------------------------------------------------- */

export interface ToolCallItem {
  kind: "tool_call";
  event: TraceEvent;
  call: ToolCallPayloadView;
  resultEvent: TraceEvent | null;
  result: ToolResultPayloadView | null;
  faulted: boolean;
  assertions: AssertionResult[];
}

export interface SimpleItem {
  kind: "model_response" | "fault" | "retry" | "limit_exceeded" | "error" | "system" | "user_message" | "model_request" | "other";
  event: TraceEvent;
}

export type TimelineItem = ToolCallItem | SimpleItem;

export interface TurnView {
  index: number;
  userMessage: string;
  finalResponse: string | null;
  passed: boolean | null;
  limitExceeded: boolean;
  steps: number | null;
  items: TimelineItem[];
  /** Assertions for this turn that are not tied to a specific tool call. */
  standaloneAssertions: AssertionResult[];
  allAssertions: AssertionResult[];
}

export interface TraceModel {
  turns: TurnView[];
  /** Scenario-scoped assertions (`turn_index === null`). */
  scenarioAssertions: AssertionResult[];
  /** Events that carry no turn index and belong to no turn (setup/teardown). */
  orphanEvents: TraceEvent[];
  totals: {
    toolCalls: number;
    faults: number;
    assertionsPassed: number;
    assertionsTotal: number;
  };
}

function itemKind(event: TraceEvent): SimpleItem["kind"] {
  switch (event.type) {
    case "model_response":
    case "fault":
    case "retry":
    case "limit_exceeded":
    case "error":
    case "system":
    case "user_message":
    case "model_request":
      return event.type;
    default:
      return "other";
  }
}

/** Does this assertion result reference the given tool call? */
function assertionReferencesCall(assertion: AssertionResult, call: ToolCallItem): boolean {
  const details = assertion.details;
  if (!details) return false;

  const seqKeys = ["seq", "call_seq", "matched_seq", "event_seq"];
  for (const key of seqKeys) {
    const value = details[key];
    if (typeof value === "number" && value === call.event.seq) {
      return true;
    }
  }
  const seqList = readArray(details, "seqs", "matched_seqs", "call_seqs");
  if (seqList?.some((value) => typeof value === "number" && value === call.event.seq)) {
    return true;
  }

  const callId = call.call.toolCallId;
  if (callId) {
    const detailId = readString(details, "tool_call_id", "call_id");
    if (detailId === callId) {
      return true;
    }
  }

  const toolName = readString(details, "tool", "tool_name");
  if (toolName && toolName === call.call.name) {
    return true;
  }

  // `order` assertions name two tools.
  for (const key of ["before", "after"]) {
    const side = readObject(details, key);
    if (side && readString(side, "tool") === call.call.name) {
      return true;
    }
  }
  const calls = readArray(details, "calls");
  if (
    calls?.some((entry) => {
      if (entry && typeof entry === "object" && !Array.isArray(entry)) {
        return readString(entry as EventPayload, "tool", "name") === call.call.name;
      }
      return false;
    })
  ) {
    return true;
  }

  return false;
}

export function buildTraceModel(trace: AttemptTrace): TraceModel {
  const events = [...(trace.events ?? [])].sort((a, b) => a.seq - b.seq);
  const assertions = trace.assertion_results ?? [];

  const declaredTurns: TraceTurn[] = trace.turns ?? [];
  const turnIndexes = new Set<number>();
  for (const turn of declaredTurns) turnIndexes.add(turn.index);
  for (const event of events) {
    if (event.turn_index !== null && event.turn_index !== undefined) {
      turnIndexes.add(event.turn_index);
    }
  }

  // Pair each tool_call with its tool_result.
  const resultEvents = events.filter((event) => event.type === "tool_result");
  const consumed = new Set<number>();

  function findResult(callEvent: TraceEvent, call: ToolCallPayloadView): TraceEvent | null {
    if (call.toolCallId) {
      const byId = resultEvents.find((candidate) => {
        if (consumed.has(candidate.seq)) return false;
        const view = toolResultView(candidate);
        return view.toolCallId === call.toolCallId;
      });
      if (byId) {
        consumed.add(byId.seq);
        return byId;
      }
    }
    const byOrder = resultEvents.find((candidate) => {
      if (consumed.has(candidate.seq) || candidate.seq < callEvent.seq) return false;
      const view = toolResultView(candidate);
      return view.name === null || view.name === call.name;
    });
    if (byOrder) {
      consumed.add(byOrder.seq);
      return byOrder;
    }
    return null;
  }

  const turns: TurnView[] = [];
  let toolCallCount = 0;
  let faultCount = 0;

  for (const index of [...turnIndexes].sort((a, b) => a - b)) {
    const declared = declaredTurns.find((turn) => turn.index === index);
    const turnEvents = events.filter((event) => event.turn_index === index);
    const turnAssertions = assertions.filter((assertion) => assertion.turn_index === index);

    const items: TimelineItem[] = [];
    const attached = new Set<AssertionResult>();

    for (const event of turnEvents) {
      if (event.type === "tool_call") {
        const call = toolCallView(event);
        const resultEvent = findResult(event, call);
        const result = resultEvent ? toolResultView(resultEvent) : null;
        const faulted = call.faulted || (result?.faulted ?? false);
        const item: ToolCallItem = {
          kind: "tool_call",
          event,
          call,
          resultEvent,
          result,
          faulted,
          assertions: [],
        };
        item.assertions = turnAssertions.filter((assertion) => assertionReferencesCall(assertion, item));
        for (const assertion of item.assertions) {
          attached.add(assertion);
        }
        toolCallCount += 1;
        if (faulted) faultCount += 1;
        items.push(item);
        continue;
      }
      if (event.type === "tool_result") {
        // Rendered next to its call.
        if (consumed.has(event.seq)) continue;
        items.push({ kind: "other", event });
        continue;
      }
      if (event.type === "fault") {
        faultCount += 1;
      }
      items.push({ kind: itemKind(event), event });
    }

    const userMessage =
      declared?.user_message ??
      readString(turnEvents.find((event) => event.type === "user_message")?.payload, "content", "message", "user") ??
      "";

    turns.push({
      index,
      userMessage,
      finalResponse: declared?.final_response ?? null,
      passed: declared?.passed ?? null,
      limitExceeded: declared?.limit_exceeded ?? turnEvents.some((event) => event.type === "limit_exceeded"),
      steps: turnEvents.filter((event) => event.type === "model_response").length,
      items,
      standaloneAssertions: turnAssertions.filter((assertion) => !attached.has(assertion)),
      allAssertions: turnAssertions,
    });
  }

  const scenarioAssertions = assertions.filter(
    (assertion) => assertion.turn_index === null || assertion.turn_index === undefined,
  );

  return {
    turns,
    scenarioAssertions,
    orphanEvents: events.filter((event) => event.turn_index === null || event.turn_index === undefined),
    totals: {
      toolCalls: toolCallCount,
      faults: faultCount,
      assertionsPassed: assertions.filter((assertion) => assertion.passed).length,
      assertionsTotal: assertions.length,
    },
  };
}
