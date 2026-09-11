"use client";

import * as React from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  CornerDownRight,
  MessageSquare,
  Repeat,
  ServerCrash,
  Terminal,
  Scissors,
  TimerOff,
  User,
  Wrench,
} from "lucide-react";

import { AssertionGroup, AssertionRow } from "@/components/trace/assertion-row";
import { AttemptVerdictBadge, JsonBlock, PassFailBadge, Stat, StatusBadge } from "@/components/domain";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { formatCost, formatMs, formatTokens, prettyJson } from "@/lib/format";
import { modelResponseText, readNumber, readString, type SimpleItem, type TimelineItem, type ToolCallItem, type TraceModel, type TurnView } from "@/lib/trace";
import type { AttemptTrace } from "@/lib/types";
import { cn } from "@/lib/utils";

export function TraceView({ trace, model }: { trace: AttemptTrace; model: TraceModel }) {
  const [showRaw, setShowRaw] = React.useState(false);

  return (
    <div className="space-y-6">
      <TraceHeader trace={trace} model={model} showRaw={showRaw} onShowRawChange={setShowRaw} />

      {model.turns.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="p-8 text-center text-sm text-muted-foreground">
            {trace.attempt.status === "queued" || trace.attempt.status === "running"
              ? "This attempt has not produced any events yet. The page will show them once it has run."
              : "This attempt recorded no events."}
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-8">
          {model.turns.map((turn) => (
            <TurnSection key={turn.index} turn={turn} showRaw={showRaw} />
          ))}
        </div>
      )}

      {model.scenarioAssertions.length > 0 ? (
        <Card>
          <CardContent className="space-y-3 p-5">
            <AssertionGroup title="Scenario-scoped assertions" assertions={model.scenarioAssertions} showRaw={showRaw} />
          </CardContent>
        </Card>
      ) : null}

      {showRaw && model.orphanEvents.length > 0 ? (
        <Card>
          <CardContent className="space-y-2 p-5">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Events outside any turn
            </p>
            <JsonBlock value={model.orphanEvents} />
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                      */
/* -------------------------------------------------------------------------- */

function TraceHeader({
  trace,
  model,
  showRaw,
  onShowRawChange,
}: {
  trace: AttemptTrace;
  model: TraceModel;
  showRaw: boolean;
  onShowRawChange: (value: boolean) => void;
}) {
  const [open, setOpen] = React.useState(true);

  return (
    <Card>
      <CardContent className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              className="flex items-center gap-2 text-left"
              aria-expanded={open}
            >
              <ChevronDown className={cn("h-4 w-4 shrink-0 transition-transform", !open && "-rotate-90")} />
              <span className="truncate text-lg font-semibold">
                {trace.scenario_title ?? trace.scenario_id}
              </span>
            </button>
            <div className="mt-2 flex flex-wrap items-center gap-2 pl-6">
              <Badge variant="outline" className="font-mono text-[11px]">
                {trace.scenario_id}
              </Badge>
              <Badge variant="secondary">{trace.run.model_key}</Badge>
              <Badge variant="muted">repetition {trace.attempt.repetition}</Badge>
              <StatusBadge status={trace.attempt.status} passed={trace.attempt.passed ?? null} />
              {trace.attempt.critical_failure ? (
                <Badge variant="destructive">
                  <AlertTriangle className="h-3 w-3" />
                  critical failure
                </Badge>
              ) : null}
              {trace.attempt.run_id ? (
                <Button variant="link" size="sm" className="h-auto p-0" asChild>
                  <Link href={`/runs/${trace.attempt.run_id}`}>Back to run</Link>
                </Button>
              ) : null}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Switch id="raw-json" checked={showRaw} onCheckedChange={onShowRawChange} />
            <Label htmlFor="raw-json" className="cursor-pointer text-sm">
              Raw event JSON
            </Label>
          </div>
        </div>

        {open ? (
          <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Stat
              label="Verdict"
              value={
                <AttemptVerdictBadge
                  status={trace.attempt.status}
                  passed={trace.attempt.passed}
                />
              }
            />
            <Stat label="Cost" value={formatCost(trace.attempt.cost_usd)} hint={trace.attempt.cost_usd === null ? "no pricing data" : undefined} />
            <Stat
              label="Tokens"
              value={`${formatTokens(trace.attempt.input_tokens ?? null)} / ${formatTokens(trace.attempt.output_tokens ?? null)}`}
              hint="input / output"
            />
            <Stat label="Duration" value={formatMs(trace.attempt.duration_ms ?? null)} />
            <Stat label="Tool calls" value={model.totals.toolCalls} hint={model.totals.faults > 0 ? `${model.totals.faults} faulted` : undefined} />
            <Stat
              label="Assertions"
              value={`${model.totals.assertionsPassed}/${model.totals.assertionsTotal}`}
              hint="passed"
            />
          </div>
        ) : null}

        {trace.attempt.error ? (
          <p className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 font-mono text-xs text-destructive dark:text-fail">
            {trace.attempt.error}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Turn timeline                                                               */
/* -------------------------------------------------------------------------- */

function TurnSection({ turn, showRaw }: { turn: TurnView; showRaw: boolean }) {
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">Turn {turn.index + 1}</h2>
        <PassFailBadge passed={turn.passed} label={turn.passed === null ? "in progress" : undefined} />
        {turn.limitExceeded ? (
          <Badge variant="warn">
            <TimerOff className="h-3 w-3" />
            limit exceeded
          </Badge>
        ) : null}
        {turn.steps !== null ? <Badge variant="muted">{turn.steps} steps</Badge> : null}
      </div>

      {turn.userMessage ? (
        <TimelineCard icon={<User className="h-4 w-4" />} tone="user" title="User">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{turn.userMessage}</p>
        </TimelineCard>
      ) : null}

      <div className="space-y-3 border-l pl-4">
        {turn.items.map((item) => (
          <TimelineEntry key={`${item.kind}-${item.event.seq}`} item={item} showRaw={showRaw} />
        ))}
      </div>

      {turn.finalResponse ? (
        <TimelineCard icon={<MessageSquare className="h-4 w-4" />} tone="assistant" title="Final response">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{turn.finalResponse}</p>
        </TimelineCard>
      ) : null}

      {turn.standaloneAssertions.length > 0 ? (
        <div className="pt-1">
          <AssertionGroup title="Turn assertions" assertions={turn.standaloneAssertions} showRaw={showRaw} />
        </div>
      ) : null}
    </section>
  );
}

function TimelineEntry({ item, showRaw }: { item: TimelineItem; showRaw: boolean }) {
  if (item.kind === "tool_call") {
    return <ToolCallEntry item={item} showRaw={showRaw} />;
  }
  return <SimpleEntry item={item} showRaw={showRaw} />;
}

function SimpleEntry({ item, showRaw }: { item: SimpleItem; showRaw: boolean }) {
  const { event } = item;

  if (item.kind === "model_response") {
    const text = modelResponseText(event);
    return (
      <TimelineCard
        icon={<Bot className="h-4 w-4" />}
        tone="assistant"
        title="Model response"
        meta={[
          event.latency_ms != null ? formatMs(event.latency_ms) : null,
          event.input_tokens != null || event.output_tokens != null
            ? `${formatTokens(event.input_tokens ?? 0)} in / ${formatTokens(event.output_tokens ?? 0)} out`
            : null,
        ]}
      >
        {text ? (
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{text}</p>
        ) : (
          <p className="text-sm italic text-muted-foreground">No text content (tool calls only).</p>
        )}
        {showRaw ? <JsonBlock value={event} className="mt-3" /> : null}
      </TimelineCard>
    );
  }

  if (item.kind === "fault") {
    return (
      <TimelineCard icon={<ServerCrash className="h-4 w-4" />} tone="fault" title="Injected fault">
        <p className="text-sm">
          <span className="font-mono">{readString(event.payload, "tool", "tool_name") ?? "tool"}</span>
          {readNumber(event.payload, "on_call") !== null ? ` · call #${readNumber(event.payload, "on_call")}` : ""}
        </p>
        <JsonBlock value={showRaw ? event : event.payload} className="mt-2" maxHeight="max-h-48" />
      </TimelineCard>
    );
  }

  if (item.kind === "retry") {
    return (
      <TimelineCard icon={<Repeat className="h-4 w-4" />} tone="warn" title="Provider retry">
        <p className="text-sm text-muted-foreground">
          {readString(event.payload, "reason", "error") ?? "Retried after a transient provider error."}
        </p>
        {showRaw ? <JsonBlock value={event} className="mt-2" maxHeight="max-h-48" /> : null}
      </TimelineCard>
    );
  }

  if (item.kind === "limit_exceeded") {
    return (
      <TimelineCard icon={<TimerOff className="h-4 w-4" />} tone="warn" title="Limit exceeded">
        <p className="text-sm">{readString(event.payload, "reason", "limit") ?? "The turn hit a configured limit."}</p>
        {showRaw ? <JsonBlock value={event} className="mt-2" maxHeight="max-h-48" /> : null}
      </TimelineCard>
    );
  }

  if (item.kind === "truncated") {
    // Not a harness limit: max_tokens cut the model off mid-response, so what
    // looks like a decision to stop may be an interruption (D35).
    return (
      <TimelineCard icon={<Scissors className="h-4 w-4" />} tone="warn" title="Response truncated">
        <p className="text-sm">
          {readString(event.payload, "detail") ?? "The response hit max_tokens."}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          {readNumber(event.payload, "output_tokens") ?? "?"} completion token(s)
          {readNumber(event.payload, "reasoning_tokens") !== null
            ? `, ${readNumber(event.payload, "reasoning_tokens")} of them reasoning`
            : ""}
          .
        </p>
        {showRaw ? <JsonBlock value={event} className="mt-2" maxHeight="max-h-48" /> : null}
      </TimelineCard>
    );
  }

  if (item.kind === "error") {
    return (
      <TimelineCard icon={<AlertTriangle className="h-4 w-4" />} tone="fault" title="Error">
        <p className="whitespace-pre-wrap font-mono text-xs">
          {readString(event.payload, "message", "error") ?? prettyJson(event.payload)}
        </p>
      </TimelineCard>
    );
  }

  if (item.kind === "system") {
    return (
      <TimelineCard icon={<Terminal className="h-4 w-4" />} tone="muted" title="System prompt">
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">
          {readString(event.payload, "content", "system_prompt", "message") ?? prettyJson(event.payload)}
        </p>
      </TimelineCard>
    );
  }

  if (item.kind === "user_message") {
    // Already rendered as the turn header.
    return showRaw ? (
      <TimelineCard icon={<User className="h-4 w-4" />} tone="user" title="User message event">
        <JsonBlock value={event} maxHeight="max-h-48" />
      </TimelineCard>
    ) : null;
  }

  if (item.kind === "model_request") {
    return showRaw ? (
      <TimelineCard icon={<CornerDownRight className="h-4 w-4" />} tone="muted" title="Model request">
        <JsonBlock value={event} />
      </TimelineCard>
    ) : null;
  }

  return showRaw ? (
    <TimelineCard icon={<CornerDownRight className="h-4 w-4" />} tone="muted" title={event.type}>
      <JsonBlock value={event} maxHeight="max-h-48" />
    </TimelineCard>
  ) : null;
}

function ToolCallEntry({ item, showRaw }: { item: ToolCallItem; showRaw: boolean }) {
  const { call, result, faulted } = item;
  const failed = result !== null && !result.ok;

  return (
    <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
      <TimelineCard
        icon={<Wrench className="h-4 w-4" />}
        tone={faulted ? "fault" : failed ? "warn" : "tool"}
        title={
          <span className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-sm font-semibold">{call.name}</code>
            {faulted ? (
              <Badge variant="destructive">
                <ServerCrash className="h-3 w-3" />
                fault injected
              </Badge>
            ) : null}
            {failed && !faulted ? <Badge variant="warn">tool error</Badge> : null}
            {call.parseError ? <Badge variant="destructive">unparsable arguments</Badge> : null}
          </span>
        }
        meta={[`seq ${item.event.seq}`, item.event.latency_ms != null ? formatMs(item.event.latency_ms) : null]}
      >
        <div className="space-y-3">
          <div>
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Arguments</p>
            {call.parseError ? (
              <>
                <p className="mb-1 font-mono text-xs text-fail">{call.parseError}</p>
                <JsonBlock value={call.rawArguments ?? call.arguments} maxHeight="max-h-48" />
              </>
            ) : (
              <JsonBlock value={call.arguments} maxHeight="max-h-64" />
            )}
          </div>

          <div>
            <p className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Result
              {result ? (
                <Badge variant={result.ok ? "pass" : "fail"} className="normal-case">
                  {result.ok ? "ok" : (result.error ?? "error")}
                </Badge>
              ) : (
                <Badge variant="muted" className="normal-case">
                  pending
                </Badge>
              )}
            </p>
            {result ? (
              <JsonBlock value={result.content} maxHeight="max-h-64" />
            ) : (
              <p className="text-sm italic text-muted-foreground">No result recorded for this call.</p>
            )}
          </div>

          {showRaw ? (
            <div>
              <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Raw events</p>
              <JsonBlock value={item.resultEvent ? [item.event, item.resultEvent] : [item.event]} />
            </div>
          ) : null}
        </div>
      </TimelineCard>

      <div className="space-y-2">
        {item.assertions.length === 0 ? (
          <div className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
            No assertion referenced this call.
          </div>
        ) : (
          item.assertions.map((assertion) => (
            <AssertionRow key={`${assertion.assertion_id}-${item.event.seq}`} assertion={assertion} showRaw={showRaw} />
          ))
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Card shell                                                                  */
/* -------------------------------------------------------------------------- */

type Tone = "user" | "assistant" | "tool" | "fault" | "warn" | "muted";

const TONE_CLASS: Record<Tone, string> = {
  user: "border-l-4 border-l-primary/60",
  assistant: "border-l-4 border-l-secondary-foreground/30",
  tool: "border-l-4 border-l-ring/50",
  fault: "border-l-4 border-l-fail bg-fail/5",
  warn: "border-l-4 border-l-warn bg-warn/5",
  muted: "border-l-4 border-l-muted",
};

function TimelineCard({
  icon,
  title,
  tone,
  meta,
  children,
}: {
  icon: React.ReactNode;
  title: React.ReactNode;
  tone: Tone;
  meta?: (string | null)[];
  children: React.ReactNode;
}) {
  const metaItems = (meta ?? []).filter((entry): entry is string => Boolean(entry));
  return (
    <Card className={cn(TONE_CLASS[tone])}>
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-muted-foreground">
            {icon}
            <span className="text-sm font-medium text-foreground">{title}</span>
          </div>
          {metaItems.length > 0 ? (
            <div className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
              {metaItems.map((entry) => (
                <span key={entry}>{entry}</span>
              ))}
            </div>
          ) : null}
        </div>
        {children}
      </CardContent>
    </Card>
  );
}
