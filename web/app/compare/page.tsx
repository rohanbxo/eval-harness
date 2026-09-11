import type { Metadata } from "next";
import Link from "next/link";
import { ArrowLeftRight, ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";

import { AxisScoreList, ConfigHashBadge, StatusBadge } from "@/components/domain";
import { ApiErrorState, EmptyState, PageHeading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getCompare, getRuns } from "@/lib/api";
import { AXES, type Axis, type Run } from "@/lib/types";
import { formatCost, formatDateTime, formatMs, formatPercent, subtract } from "@/lib/format";
import { cn } from "@/lib/utils";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Compare" };

/** A delta cell: green when b improved on a, red when it regressed. */
function Delta({
  value,
  render,
  invert = false,
}: {
  value: number | null;
  render: (n: number) => string;
  invert?: boolean;
}) {
  if (value === null) {
    return <span className="text-muted-foreground">unknown</span>;
  }
  if (Math.abs(value) < 1e-9) {
    return (
      <span className="inline-flex items-center gap-1 text-muted-foreground">
        <Minus className="h-3 w-3" />
        {render(0)}
      </span>
    );
  }
  const better = invert ? value < 0 : value > 0;
  const Icon = value > 0 ? ArrowUpRight : ArrowDownRight;
  return (
    <span className={cn("inline-flex items-center gap-1 font-medium tabular-nums", better ? "text-pass" : "text-fail")}>
      <Icon className="h-3 w-3" />
      {value > 0 ? "+" : "-"}
      {render(Math.abs(value))}
    </span>
  );
}

function runLabel(run: Run): string {
  return `${run.model_key ?? run.model_key} · ${run.id.slice(0, 8)} · ${formatDateTime(run.created_at)}`;
}

/** One side of a per-scenario comparison, regrouped from the flat API diff. */
interface CompareScenarioSide {
  config_hash: string;
  pass_at_1: number | null;
  pass_hat_k: number | null;
  axis_scores: Record<string, number>;
  cost_usd: number | null;
  latency_p50_ms: number | null;
  /** The compare endpoint reports p50 only; kept so the column can stay. */
  latency_p95_ms: number | null;
}

function axisDelta(a: CompareScenarioSide, b: CompareScenarioSide, axis: Axis): number | null {
  return subtract(b.axis_scores?.[axis] ?? null, a.axis_scores?.[axis] ?? null);
}

export default async function ComparePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const runA = typeof params.run_a === "string" ? params.run_a : "";
  const runB = typeof params.run_b === "string" ? params.run_b : "";

  const runsResult = await getRuns({ page: 1 });
  const compareResult = runA && runB ? await getCompare(runA, runB) : null;

  return (
    <>
      <PageHeading
        title="Compare runs"
        description="Per-scenario deltas for pass@1, pass^k, axis scores, cost and latency, plus every assertion whose verdict flipped."
      />

      {!runsResult.ok ? (
        <ApiErrorState error={runsResult.error} what="the run list" className="mb-6" />
      ) : runsResult.data.items.length < 2 ? (
        <EmptyState
          title="At least two runs are needed to compare"
          description="Launch another run and come back."
          icon={<ArrowLeftRight className="h-5 w-5" />}
          action={
            <Button size="sm" asChild>
              <Link href="/runs/new">Launch a run</Link>
            </Button>
          }
        />
      ) : (
        <form method="get" action="/compare" className="mb-6 flex flex-wrap items-end gap-3">
          <div className="min-w-[18rem] flex-1">
            <label htmlFor="run_a" className="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">
              Run A (baseline)
            </label>
            <Select id="run_a" name="run_a" defaultValue={runA}>
              <option value="">Select a run…</option>
              {runsResult.data.items.map((run) => (
                <option key={run.id} value={run.id}>
                  {runLabel(run)}
                </option>
              ))}
            </Select>
          </div>
          <div className="min-w-[18rem] flex-1">
            <label htmlFor="run_b" className="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">
              Run B
            </label>
            <Select id="run_b" name="run_b" defaultValue={runB}>
              <option value="">Select a run…</option>
              {runsResult.data.items.map((run) => (
                <option key={run.id} value={run.id}>
                  {runLabel(run)}
                </option>
              ))}
            </Select>
          </div>
          <Button type="submit" size="sm">
            Compare
          </Button>
        </form>
      )}

      {compareResult === null ? (
        runsResult.ok && runsResult.data.items.length >= 2 ? (
          <EmptyState
            title="Pick two runs"
            description="Deltas are computed by the API so both sides always come from the same query."
            icon={<ArrowLeftRight className="h-5 w-5" />}
          />
        ) : null
      ) : !compareResult.ok ? (
        <ApiErrorState error={compareResult.error} what="the comparison" />
      ) : (
        <CompareBody
          data={compareResult.data}
        />
      )}
    </>
  );
}

function RunColumn({ run, side }: { run: Run; side: "A" | "B" }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Badge variant={side === "A" ? "muted" : "secondary"}>Run {side}</Badge>
          {run.model_key ?? run.model_key}
        </CardTitle>
        <CardDescription className="font-mono text-xs">{run.id}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={run.status} />
          <Badge variant="muted">k = {run.k}</Badge>
          <Badge variant="muted">{formatDateTime(run.created_at)}</Badge>
        </div>
        <dl className="grid grid-cols-2 gap-2 text-sm">
          <div>
            <dt className="text-xs text-muted-foreground">pass@1</dt>
            <dd className="tabular-nums">{formatPercent(run.summary?.pass_at_1 ?? null, 1)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">pass^k</dt>
            <dd className="tabular-nums">{formatPercent(run.summary?.pass_hat_k ?? null, 1)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">cost</dt>
            <dd className="tabular-nums">{formatCost(run.summary?.cost_usd ?? null)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">latency p50</dt>
            <dd className="tabular-nums">{formatMs(run.summary?.latency_p50_ms ?? null)}</dd>
          </div>
        </dl>
        <AxisScoreList scores={run.summary?.axis_scores} />
        <Button variant="outline" size="sm" asChild>
          <Link href={`/runs/${run.id}`}>Open run</Link>
        </Button>
      </CardContent>
    </Card>
  );
}

function CompareBody({
  data,
}: {
  data: import("@/lib/types").CompareResponse;
}) {
  const { run_a: runA, run_b: runB } = data;

  // The API reports each metric as a flat `_a`/`_b` pair; the table below reads
  // one side at a time, so regroup once here rather than at every cell.
  const scenarios = (data.scenarios ?? []).map((diff) => ({
    scenario_id: diff.scenario_id,
    config_changed: diff.config_changed,
    a: {
      config_hash: diff.config_hash_a,
      pass_at_1: diff.pass_at_1_a ?? null,
      pass_hat_k: diff.pass_hat_k_a ?? null,
      axis_scores: diff.axis_scores_a ?? {},
      cost_usd: diff.cost_usd_a ?? null,
      latency_p50_ms: diff.latency_p50_ms_a ?? null,
      latency_p95_ms: null,
    } satisfies CompareScenarioSide,
    b: {
      config_hash: diff.config_hash_b,
      pass_at_1: diff.pass_at_1_b ?? null,
      pass_hat_k: diff.pass_hat_k_b ?? null,
      axis_scores: diff.axis_scores_b ?? {},
      cost_usd: diff.cost_usd_b ?? null,
      latency_p50_ms: diff.latency_p50_ms_b ?? null,
      latency_p95_ms: null,
    } satisfies CompareScenarioSide,
  }));
  const flipped = data.flipped_assertions ?? [];

  return (
    <div className="space-y-6">
      <div className="grid gap-4 lg:grid-cols-2">
        <RunColumn run={runA} side="A" />
        <RunColumn run={runB} side="B" />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Per-scenario deltas</CardTitle>
          <CardDescription>B minus A. Cost and latency deltas are better when they go down.</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {scenarios.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">These runs share no scenarios.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Scenario</TableHead>
                  <TableHead className="text-right">pass@1</TableHead>
                  <TableHead className="text-right">pass^k</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                  <TableHead className="text-right">p50</TableHead>
                  <TableHead className="text-right">p95</TableHead>
                  <TableHead>Axes (Δ)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {scenarios.map((row) => {
                  const configChanged =
                    row.config_changed ??
                    Boolean(row.a.config_hash && row.b.config_hash && row.a.config_hash !== row.b.config_hash);
                  return (
                    <TableRow key={row.scenario_id}>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <Link href={`/scenarios/${row.scenario_id}`} className="font-mono text-xs hover:underline">
                            {row.scenario_id}
                          </Link>
                          {configChanged ? (
                            <ConfigHashBadge
                              hash={row.b.config_hash ?? null}
                              changed
                              title={`Run A used ${row.a.config_hash ?? "unknown"}, run B used ${row.b.config_hash ?? "unknown"}. These results are not directly comparable.`}
                            />
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="tabular-nums">{formatPercent(row.b.pass_at_1, 0)}</div>
                        <Delta value={subtract(row.b.pass_at_1, row.a.pass_at_1)} render={(n) => formatPercent(n, 0)} />
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="tabular-nums">{formatPercent(row.b.pass_hat_k, 0)}</div>
                        <Delta value={subtract(row.b.pass_hat_k, row.a.pass_hat_k)} render={(n) => formatPercent(n, 0)} />
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="tabular-nums">{formatCost(row.b.cost_usd)}</div>
                        <Delta value={subtract(row.b.cost_usd, row.a.cost_usd)} render={formatCost} invert />
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="tabular-nums">{formatMs(row.b.latency_p50_ms ?? null)}</div>
                        <Delta
                          value={subtract(row.b.latency_p50_ms ?? null, row.a.latency_p50_ms ?? null)}
                          render={formatMs}
                          invert
                        />
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="tabular-nums">{formatMs(row.b.latency_p95_ms ?? null)}</div>
                        <Delta
                          value={subtract(row.b.latency_p95_ms ?? null, row.a.latency_p95_ms ?? null)}
                          render={formatMs}
                          invert
                        />
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
                          {AXES.map((axis: Axis) => {
                            const delta = axisDelta(row.a, row.b, axis);
                            if (delta === null || Math.abs(delta) < 1e-9) return null;
                            return (
                              <span key={axis} className="inline-flex items-center gap-1">
                                <span className="font-mono text-muted-foreground">{axis}</span>
                                <Delta value={delta} render={(n) => n.toFixed(2)} />
                              </span>
                            );
                          })}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Assertions that flipped</CardTitle>
          <CardDescription>
            Assertions whose verdict differs between the two runs. These are where the behaviour actually changed.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {flipped.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">No assertion changed verdict between these runs.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Assertion</TableHead>
                  <TableHead>Scenario</TableHead>
                  <TableHead>Direction</TableHead>
                  <TableHead className="text-right">Pass rate A</TableHead>
                  <TableHead className="text-right">Pass rate B</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {flipped.map((row) => {
                  // With k > 1 an assertion can move part-way, so the endpoint
                  // classifies the move rather than reporting a single verdict.
                  const improved = row.direction === "fixed" || row.direction === "improved";
                  return (
                    <TableRow key={`${row.scenario_id}-${row.assertion_id}`} className={cn(improved ? "bg-pass/5" : "bg-fail/5")}>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <code className="font-mono text-xs font-semibold">{row.assertion_id}</code>
                          <div className="flex flex-wrap gap-1">
                            {row.type ? (
                              <Badge variant="muted" className="font-mono text-[11px]">
                                {row.type}
                              </Badge>
                            ) : null}
                            {row.axis ? (
                              <Badge variant="outline" className="font-mono text-[11px]">
                                {row.axis}
                              </Badge>
                            ) : null}
                            {row.severity ? (
                              <Badge
                                variant={row.severity === "critical" ? "destructive" : "secondary"}
                                className="font-mono text-[11px]"
                              >
                                {row.severity}
                              </Badge>
                            ) : null}
                          </div>
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{row.scenario_id}</TableCell>
                      <TableCell>
                        <Badge variant={improved ? "pass" : "fail"}>{row.direction}</Badge>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatPercent(row.pass_rate_a ?? null, 0)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatPercent(row.pass_rate_b ?? null, 0)}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
