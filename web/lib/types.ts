/**
 * The single import point for API types.
 *
 * The response shapes are aliases onto `lib/api-types.ts`, which
 * `npm run gen:api` generates from FastAPI's OpenAPI schema (SPEC 2, 10). They
 * are never hand-written: if a field here disagrees with the backend, the fix
 * is to regenerate, not to edit.
 *
 * `lib/vocabulary.ts` holds the parts that must exist at runtime -- the axis and
 * status lists the UI iterates over -- plus the JSON value types. Those mirror
 * `backend/evalharness/schema/enums.py` and are checked against the generated
 * unions below, so a drift in either direction is a type error.
 */

import type { components } from "./api-types";
import type {
  AttemptStatus as VocabAttemptStatus,
  Axis as VocabAxis,
  EventType as VocabEventType,
  RunStatus as VocabRunStatus,
  Scope as VocabScope,
  Severity as VocabSeverity,
} from "./vocabulary";

export type {
  AssertionType,
  AttemptStatus,
  Axis,
  EventType,
  JsonObject,
  JsonValue,
  MockKind,
  RunStatus,
  Scope,
  Severity,
} from "./vocabulary";
export {
  ASSERTION_TYPES,
  ATTEMPT_STATUSES,
  AXES,
  EVENT_TYPES,
  RUN_STATUSES,
  SCOPES,
  SEVERITIES,
} from "./vocabulary";

type Schemas = components["schemas"];

/* ------------------------------------------------------------------ system */

export type HealthResponse = Schemas["HealthResponse"];

/* --------------------------------------------------------------- scenarios */

export type ScenarioListItem = Schemas["ScenarioSummary"];
export type ScenarioListResponse = Schemas["ScenarioListResponse"];
export type ScenarioDetail = Schemas["ScenarioDetail"];
export type ScenarioDefinition = Schemas["Scenario"];
export type ScenarioTool = Schemas["ToolDefinition"];
export type ScenarioTurn = Schemas["Turn"];
export type ScenarioFault = Schemas["Fault"];
export type ScenarioLimits = Schemas["Limits"];
export type ScenarioFixture = Schemas["FixtureFile"];

/* ------------------------------------------------------------------ models */

export type ModelEntry = Schemas["ModelInfo"];
export type ModelListResponse = Schemas["ModelListResponse"];

/* -------------------------------------------------------------------- runs */

export type Run = Schemas["Run"];
export type RunDetail = Schemas["RunDetail"];
export type RunListPage = Schemas["RunListResponse"];
export type RunSummary = Schemas["RunSummary"];
export type PerScenarioSummary = Schemas["ScenarioStats"];
export type CreateRunRequest = Schemas["RunCreate"];
export type CancelResponse = Schemas["CancelResponse"];
export type Attempt = Schemas["AttemptSummary"];

/**
 * Axis name -> score. The backend only includes axes a run actually measured,
 * so a value is `null` only for scores the UI derives (a mean over cells where
 * nothing measured that axis). API payloads always carry numbers.
 */
export type AxisScores = Record<string, number | null>;

/* ------------------------------------------------------------------- trace */

export type AttemptTrace = Schemas["AttemptTrace"];
export type TraceEvent = Schemas["EventOut"];
export type TraceTurn = Schemas["TurnOut"];
export type AssertionResult = Schemas["AssertionResultOut"];

/* ------------------------------------------------------------- leaderboard */

export type LeaderboardResponse = Schemas["LeaderboardResponse"];
export type LeaderboardRow = Schemas["LeaderboardRow"];
export type LeaderboardCell = Schemas["LeaderboardCell"];
export type LeaderboardScenario = Schemas["LeaderboardScenario"];

/* ----------------------------------------------------------------- compare */

export type CompareResponse = Schemas["CompareResponse"];
export type ScenarioDiff = Schemas["ScenarioDiff"];
export type FlippedAssertion = Schemas["AssertionFlip"];

/* --------------------------------------------------------------------- SSE */

export type ProgressEvent = Schemas["ProgressEvent"];
export type ProgressEventType = ProgressEvent["type"];

/**
 * The stream carries one `ProgressEvent` shape for every event name; the
 * discriminant is `type`. Kept as an alias so call sites read as a union.
 */
export type RunStreamEvent = ProgressEvent;

/* ------------------------------------------------- vocabulary drift guards */

/**
 * Compile-time proof that the runtime lists in `vocabulary.ts` still match the
 * generated unions in both directions. If the backend adds an axis, a severity
 * or an event type and `vocabulary.ts` does not follow, one of these stops
 * compiling — which is the point.
 */
type Exact<A extends B, B extends C, C = A> = true;

export type VocabularyIsInSync = [
  Exact<VocabAxis, Schemas["Axis"]>,
  Exact<Schemas["Axis"], VocabAxis>,
  Exact<VocabSeverity, Schemas["Severity"]>,
  Exact<Schemas["Severity"], VocabSeverity>,
  Exact<VocabScope, Schemas["Scope"]>,
  Exact<Schemas["Scope"], VocabScope>,
  Exact<VocabEventType, Schemas["EventType"]>,
  Exact<Schemas["EventType"], VocabEventType>,
  Exact<VocabRunStatus, Schemas["RunStatus"]>,
  Exact<Schemas["RunStatus"], VocabRunStatus>,
  Exact<VocabAttemptStatus, Schemas["AttemptStatus"]>,
  Exact<Schemas["AttemptStatus"], VocabAttemptStatus>,
];
