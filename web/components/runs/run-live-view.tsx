"use client";

import * as React from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Loader2, Radio, RefreshCw, WifiOff } from "lucide-react";

import { AxisScoreList, ConfigHashBadge, PassFailBadge, Stat, StatusBadge } from "@/components/domain";
import { EmptyState, InlineError } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useRunStream } from "@/hooks/use-run-stream";
import { cancelRun, fetchRun } from "@/lib/client-api";
import { formatCost, formatDateTime, formatMs, formatPercent, formatScore, formatTokens } from "@/lib/format";
import type { Attempt, AttemptStatus, RunDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

const TERMINAL = new Set(["completed", "failed", "errored", "cancelled"]);

const CELL_CLASS: Record<AttemptStatus, string> = {
  queued: "border-dashed bg-muted/40 text-muted-foreground",
  running: "border-warn bg-warn/15 text-foreground",
  completed: "border-border bg-card",
  failed: "border-fail bg-fail/15",
  // Hatched warning, not red: nothing is known about the model here.
  errored: "border-warn border-dashed bg-warn/15 text-foreground",
  cancelled: "border-border bg-muted/60 text-muted-foreground",
};

export function RunLiveView({ initialRun }: { initialRun: RunDetail }) {
  const queryClient = useQueryClient();
  const queryKey = React.useMemo(() => ["run", initialRun.id] as const, [initialRun.id]);

  const { data: run, isFetching, error, refetch } = useQuery({
    queryKey,
    queryFn: () => fetchRun(initialRun.id),
    initialData: initialRun,
    // Polling is the safety net if SSE never connects; it stops on terminal states.
    refetchInterval: (query) => (TERMINAL.has(query.state.data?.status ?? "") ? false : 4000),
  });

  const live = !TERMINAL.has(run.status);
  // A run row has no error column; failures live on its attempts (SPEC 8.3).
  const firstAttemptError =
    (run.attempts ?? []).find((attempt) => attempt.error)?.error ?? null;

  const streamState = useRunStream({
    runId: initialRun.id,
    enabled: live,
    onEvent: () => {
      void queryClient.invalidateQueries({ queryKey });
    },
  });

  const cancellation = useMutation({
    mutationFn: () => cancelRun(initialRun.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey });
    },
  });

  const attempts = run.attempts ?? [];
  const finished = attempts.filter((attempt) => TERMINAL.has(attempt.status)).length;
  const progress = attempts.length > 0 ? finished / attempts.length : 0;

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="flex flex-wrap items-start justify-between gap-4 p-5">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={run.status} />
              <Badge variant="secondary">{run.model_key ?? run.model_key}</Badge>
              <Badge variant="muted">k = {run.k}</Badge>
              <Badge variant="muted">
                {(run.scenario_ids ?? []).length} scenario{(run.scenario_ids ?? []).length === 1 ? "" : "s"}
              </Badge>
              {live ? (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span>
                      <Badge variant={streamState === "open" ? "pass" : "warn"}>
                        {streamState === "open" ? <Radio className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
                        {streamState === "open" ? "live" : "polling"}
                      </Badge>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>
                    {streamState === "open"
                      ? "Connected to the run's SSE stream."
                      : "The SSE stream is not connected; the page is polling the API every 4 seconds instead."}
                  </TooltipContent>
                </Tooltip>
              ) : null}
            </div>
            <p className="font-mono text-xs text-muted-foreground">{run.id}</p>
            <p className="text-sm text-muted-foreground">
              Created {formatDateTime(run.created_at)}
              {run.git_commit ? ` · commit ${run.git_commit}` : ""}
              {run.harness_version ? ` · harness ${run.harness_version}` : ""}
            </p>
          </div>

          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => void refetch()} disabled={isFetching}>
              <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
              Refresh
            </Button>
            {live ? (
              <Button
                variant="destructive"
                size="sm"
                onClick={() => cancellation.mutate()}
                disabled={cancellation.isPending}
              >
                {cancellation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Ban className="h-4 w-4" />}
                Cancel run
              </Button>
            ) : null}
          </div>
        </CardContent>
      </Card>

      {error ? (
        <InlineError message={error instanceof Error ? error.message : "Could not refresh this run."} />
      ) : null}
      {cancellation.isError ? (
        <InlineError
          message={cancellation.error instanceof Error ? cancellation.error.message : "Could not cancel this run."}
        />
      ) : null}
      {firstAttemptError ? <InlineError message={firstAttemptError} /> : null}

      {live ? (
        <div>
          <div className="mb-1 flex items-center justify-between text-sm">
            <span className="text-muted-foreground">
              {finished} of {attempts.length || (run.scenario_ids ?? []).length * run.k} attempts finished
            </span>
            <span className="tabular-nums text-muted-foreground">{formatPercent(progress)}</span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary transition-[width] duration-500"
              style={{ width: `${Math.round(progress * 100)}%` }}
            />
          </div>
        </div>
      ) : null}

      <AttemptGrid run={run} />

      <SummaryPanel run={run} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Attempt grid: scenario x repetition                                         */
/* -------------------------------------------------------------------------- */

function AttemptGrid({ run }: { run: RunDetail }) {
  const attempts = run.attempts ?? [];

  if (attempts.length === 0) {
    return (
      <EmptyState
        title={run.status === "queued" ? "Waiting for the worker to pick this run up" : "No attempts recorded"}
        description={
          run.status === "queued"
            ? "Attempts appear here as soon as Celery starts them. This page updates itself."
            : "This run finished without recording any attempts."
        }
        icon={<Loader2 className={cn("h-5 w-5", run.status === "queued" && "animate-spin")} />}
      />
    );
  }

  const scenarioIds =
    (run.scenario_ids ?? []).length > 0
      ? (run.scenario_ids ?? [])
      : [...new Set(attempts.map((a) => a.scenario_id))];
  const repetitions = Array.from({ length: Math.max(run.k, ...attempts.map((a) => a.repetition + 1)) }, (_, i) => i);
  const byKey = new Map<string, Attempt>();
  for (const attempt of attempts) {
    byKey.set(`${attempt.scenario_id}::${attempt.repetition}`, attempt);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Attempts</CardTitle>
        <CardDescription>Scenario × repetition. Click a cell to open its full trace.</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full border-separate border-spacing-1 text-sm">
            <thead>
              <tr>
                <th className="w-56 px-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Scenario
                </th>
                {repetitions.map((repetition) => (
                  <th
                    key={repetition}
                    className="px-2 text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground"
                  >
                    #{repetition + 1}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {scenarioIds.map((scenarioId) => (
                <tr key={scenarioId}>
                  <th className="px-2 py-1 text-left align-middle font-mono text-xs font-medium">
                    <div className="flex flex-col gap-1">
                      <span>{scenarioId}</span>
                      <ConfigHashBadge hash={run.config_hashes?.[scenarioId] ?? null} />
                    </div>
                  </th>
                  {repetitions.map((repetition) => {
                    const attempt = byKey.get(`${scenarioId}::${repetition}`);
                    return (
                      <td key={repetition} className="p-0">
                        <AttemptCell attempt={attempt} />
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <LegendSwatch className="border-dashed bg-muted/40" label="queued" />
          <LegendSwatch className="border-warn bg-warn/15" label="running" />
          <LegendSwatch className="border-pass bg-pass/15" label="passed" />
          <LegendSwatch className="border-fail bg-fail/15" label="failed" />
          <LegendSwatch className="border-border bg-muted/60" label="cancelled" />
        </div>
      </CardContent>
    </Card>
  );
}

function LegendSwatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={cn("inline-block h-3 w-5 rounded border", className)} />
      {label}
    </span>
  );
}

function AttemptCell({ attempt }: { attempt: Attempt | undefined }) {
  if (!attempt) {
    return (
      <div className="flex h-16 items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground">
        —
      </div>
    );
  }

  const passed = attempt.passed ?? null;
  const tone =
    attempt.status === "completed" && passed !== null
      ? passed
        ? "border-pass bg-pass/15"
        : "border-fail bg-fail/15"
      : CELL_CLASS[attempt.status];

  return (
    <Link
      href={`/attempts/${attempt.id}`}
      className={cn(
        "flex h-16 flex-col items-center justify-center gap-1 rounded-md border px-2 text-center transition-colors hover:ring-2 hover:ring-ring",
        tone,
      )}
      title={`${attempt.scenario_id} #${attempt.repetition + 1} · ${attempt.status}`}
    >
      <span className="text-xs font-medium capitalize">
        {attempt.status === "completed" ? (passed === null ? "graded" : passed ? "pass" : "fail") : attempt.status}
      </span>
      <span className="font-mono text-[10px] text-muted-foreground">
        {attempt.status === "running" ? "…" : formatMs(attempt.duration_ms ?? null)}
      </span>
      {attempt.critical_failure ? <span className="text-[10px] font-semibold text-fail">critical</span> : null}
    </Link>
  );
}

/* -------------------------------------------------------------------------- */
/* Summary                                                                     */
/* -------------------------------------------------------------------------- */

function SummaryPanel({ run }: { run: RunDetail }) {
  const summary = run.summary;

  if (!summary) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Summary</CardTitle>
          <CardDescription>
            {TERMINAL.has(run.status)
              ? "This run finished without a stored summary."
              : "The summary is computed once every attempt has finished."}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Summary
          {summary.incomplete ? <Badge variant="warn">incomplete</Badge> : null}
        </CardTitle>
        <CardDescription>
          Scored over {summary.completed_attempts ?? 0} of{" "}
          {summary.total_attempts ?? (run.attempts ?? []).length} attempts
          {summary.passed_attempts !== null && summary.passed_attempts !== undefined
            ? ` · ${summary.passed_attempts} passed`
            : ""}
          .
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {summary.incomplete ? (
          <InlineError
            tone="warn"
            message={
              `${summary.errored_attempts ?? 0} attempt(s) never produced a verdict, so these ` +
              `rates cover only the ${summary.completed_attempts ?? 0} that did ` +
              `(${formatPercent(summary.coverage ?? 0, 0)} coverage). ` +
              `pass^${run.k} is computed over the ${summary.scenarios_scored ?? 0} of ` +
              `${summary.scenarios_total ?? 0} scenarios that ran every repetition.`
            }
          />
        ) : null}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="pass@1" value={formatPercent(summary.pass_at_1, 1)} />
          <Stat label={`pass^${run.k}`} value={formatPercent(summary.pass_hat_k, 1)} />
          <Stat
            label="Cost"
            value={formatCost(summary.cost_usd)}
            hint={summary.cost_usd === null ? "pricing unknown for this model" : undefined}
          />
          <Stat label="Latency p50" value={formatMs(summary.latency_p50_ms ?? null)} />
          <Stat label="Latency p95" value={formatMs(summary.latency_p95_ms ?? null)} />
          <Stat
            label="Tokens"
            value={`${formatTokens(summary.input_tokens ?? null)} / ${formatTokens(summary.output_tokens ?? null)}`}
            hint="input / output"
          />
        </div>

        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Axis means</p>
          <AxisScoreList scores={summary.axis_scores} />
        </div>

        {summary.mean_steps_per_turn !== null && summary.mean_steps_per_turn !== undefined ? (
          <p className="text-sm text-muted-foreground">
            Mean steps per turn: <span className="font-medium tabular-nums">{formatScore(summary.mean_steps_per_turn)}</span>
          </p>
        ) : null}

        {summary.per_scenario && summary.per_scenario.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="px-2 py-2 text-left text-xs uppercase tracking-wide text-muted-foreground">Scenario</th>
                  <th className="px-2 py-2 text-right text-xs uppercase tracking-wide text-muted-foreground">pass@1</th>
                  <th className="px-2 py-2 text-right text-xs uppercase tracking-wide text-muted-foreground">
                    pass^{run.k}
                  </th>
                  <th className="px-2 py-2 text-right text-xs uppercase tracking-wide text-muted-foreground">Cost</th>
                  <th className="px-2 py-2 text-right text-xs uppercase tracking-wide text-muted-foreground">p50</th>
                </tr>
              </thead>
              <tbody>
                {summary.per_scenario.map((row) => (
                  <tr key={row.scenario_id} className="border-b last:border-0">
                    <td className="px-2 py-2 font-mono text-xs">{row.scenario_id}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{formatPercent(row.pass_at_1, 1)}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{formatPercent(row.pass_hat_k, 1)}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{formatCost(row.cost_usd)}</td>
                    <td className="px-2 py-2 text-right tabular-nums">{formatMs(row.latency_p50_ms ?? null)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        <div className="flex flex-wrap gap-2">
          {(run.attempts ?? []).map((attempt) => (
            <Link key={attempt.id} href={`/attempts/${attempt.id}`}>
              <span className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs transition-colors hover:bg-accent">
                <span className="font-mono">
                  {attempt.scenario_id} #{attempt.repetition + 1}
                </span>
                <PassFailBadge passed={attempt.passed ?? null} />
              </span>
            </Link>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
