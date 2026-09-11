"use client";

import * as React from "react";
import Link from "next/link";
import { AlertTriangle } from "lucide-react";

import { AxisBars, AxisRadar, type AxisSeries } from "@/components/charts/axis-radar";
import { ConfigHashBadge } from "@/components/domain";
import { EmptyState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatCost, formatMs, formatPercent, shortHash } from "@/lib/format";
import { AXES, type Axis, type AxisScores, type LeaderboardCell, type LeaderboardResponse } from "@/lib/types";
import { cn } from "@/lib/utils";

type SortKey = "score" | "cost" | "latency" | "name";

interface ModelRow {
  key: string;
  label: string;
  cells: Map<string, LeaderboardCell>;
  meanPassAt1: number | null;
  meanPassHatK: number | null;
  totalCost: number | null;
  meanLatency: number | null;
  /** Mean per axis across this model's cells; null where nothing measured it. */
  axisMeans: AxisScores;
  configChanged: boolean;
  /** 95% Wilson bounds for pass@1, straight from the API. */
  passAt1Low: number | null;
  passAt1High: number | null;
  notSignificantVsLeader: boolean;
}

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function cellTone(passAt1: number | null): string {
  if (passAt1 === null) return "bg-muted/40 text-muted-foreground";
  if (passAt1 >= 0.9) return "bg-pass/25";
  if (passAt1 >= 0.7) return "bg-pass/15";
  if (passAt1 >= 0.4) return "bg-warn/20";
  if (passAt1 > 0) return "bg-fail/15";
  return "bg-fail/25";
}

export function LeaderboardView({ data }: { data: LeaderboardResponse }) {
  const [sort, setSort] = React.useState<SortKey>("score");

  // The API groups cells under one row per model (SPEC 9.2). The heatmap below
  // wants them flat, plus the model and scenario axes, so derive all three here.
  const scenarios = React.useMemo(
    () =>
      (data.scenarios ?? []).map((scenario) => ({
        id: scenario.scenario_id,
        title: scenario.title,
        config_hash: scenario.current_config_hash ?? null,
      })),
    [data.scenarios],
  );
  const models = React.useMemo(
    () =>
      (data.rows ?? []).map((row) => ({
        key: row.model_key,
        display_name: row.display_name,
      })),
    [data.rows],
  );
  const cells = React.useMemo(
    () => (data.rows ?? []).flatMap((row) => row.cells ?? []),
    [data.rows],
  );

  /** Reference hash per scenario: the one the scenario list reports today. */
  const referenceHash = React.useMemo(() => {
    const map = new Map<string, string | null>();
    for (const scenario of scenarios) {
      map.set(scenario.id, scenario.config_hash ?? null);
    }
    for (const cell of cells) {
      if (!map.get(cell.scenario_id) && cell.config_hash) {
        map.set(cell.scenario_id, cell.config_hash);
      }
    }
    return map;
  }, [scenarios, cells]);

  /** Scenarios whose results span more than one config hash. */
  const mixedScenarios = React.useMemo(() => {
    const seen = new Map<string, Set<string>>();
    for (const cell of cells) {
      if (!cell.config_hash) continue;
      const set = seen.get(cell.scenario_id) ?? new Set<string>();
      set.add(cell.config_hash);
      seen.set(cell.scenario_id, set);
    }
    return new Set([...seen.entries()].filter(([, hashes]) => hashes.size > 1).map(([id]) => id));
  }, [cells]);

  const rows = React.useMemo<ModelRow[]>(() => {
    const modelKeys =
      models.length > 0 ? models.map((model) => model.key) : [...new Set(cells.map((cell) => cell.model_key))];

    return modelKeys.map((key) => {
      const label = models.find((model) => model.key === key)?.display_name ?? key;
      // The API computes the Wilson interval and the significance flag; they are
      // read here rather than recomputed, so the UI cannot disagree with the
      // numbers the run actually produced.
      const source = (data.rows ?? []).find((r) => r.model_key === key);
      const modelCells = cells.filter((cell) => cell.model_key === key);
      const byScenario = new Map<string, LeaderboardCell>();
      for (const cell of modelCells) {
        byScenario.set(cell.scenario_id, cell);
      }

      const passValues = modelCells.map((cell) => cell.pass_at_1).filter((value): value is number => value !== null);
      const hatValues = modelCells.map((cell) => cell.pass_hat_k).filter((value): value is number => value !== null);
      const costValues = modelCells.map((cell) => cell.cost_usd).filter((value): value is number => value !== null);
      const latencyValues = modelCells
        .map((cell) => cell.latency_p50_ms ?? null)
        .filter((value): value is number => value !== null && value !== undefined);

      const axisMeans: AxisScores = {};
      for (const axis of AXES) {
        const values = modelCells
          .map((cell) => cell.axis_scores?.[axis] ?? null)
          .filter((value): value is number => value !== null && value !== undefined);
        axisMeans[axis] = mean(values);
      }

      return {
        key,
        label,
        cells: byScenario,
        passAt1Low: source?.pass_at_1_low ?? null,
        passAt1High: source?.pass_at_1_high ?? null,
        notSignificantVsLeader: source?.not_significant_vs_leader ?? false,
        meanPassAt1: mean(passValues),
        meanPassHatK: mean(hatValues),
        // Cost is only meaningful if every cell reported one; a partial sum
        // would understate it, and SPEC 6.3 forbids guessing.
        totalCost: costValues.length === modelCells.length && modelCells.length > 0 ? costValues.reduce((a, b) => a + b, 0) : null,
        meanLatency: mean(latencyValues),
        axisMeans,
        configChanged: modelCells.some(
          (cell) => cell.config_hash && referenceHash.get(cell.scenario_id) && cell.config_hash !== referenceHash.get(cell.scenario_id),
        ),
      };
    });
  }, [cells, models, referenceHash, data.rows]);

  const sorted = React.useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      switch (sort) {
        case "cost": {
          if (a.totalCost === null && b.totalCost === null) return a.label.localeCompare(b.label);
          if (a.totalCost === null) return 1;
          if (b.totalCost === null) return -1;
          return a.totalCost - b.totalCost;
        }
        case "latency": {
          if (a.meanLatency === null && b.meanLatency === null) return a.label.localeCompare(b.label);
          if (a.meanLatency === null) return 1;
          if (b.meanLatency === null) return -1;
          return a.meanLatency - b.meanLatency;
        }
        case "name":
          return a.label.localeCompare(b.label);
        case "score":
        default: {
          const scoreA = a.meanPassAt1 ?? -1;
          const scoreB = b.meanPassAt1 ?? -1;
          return scoreB - scoreA;
        }
      }
    });
    return copy;
  }, [rows, sort]);

  const axisSeries: AxisSeries[] = sorted.map((row) => ({ name: row.label, scores: row.axisMeans }));

  if (rows.length === 0 || scenarios.length === 0) {
    return (
      <EmptyState
        title="No completed runs yet"
        description="The leaderboard aggregates the latest completed run per model per config hash. Launch a run to populate it."
        action={
          <Button size="sm" asChild>
            <Link href="/runs/new">Launch a run</Link>
          </Button>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
      {mixedScenarios.size > 0 ? (
        <div className="flex items-start gap-2 rounded-lg border border-warn/50 bg-warn/10 p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
          <p>
            Results for{" "}
            <span className="font-medium">
              {[...mixedScenarios].join(", ")}
            </span>{" "}
            span more than one <code className="font-mono">config_hash</code>. Cells from an older hash are badged; they
            are not comparable with the current definition and are never averaged together silently.
          </p>
        </div>
      ) : null}

      <Card>
        <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
          <div>
            <CardTitle>Models × scenarios</CardTitle>
            <CardDescription>
              Each cell shows pass@1 with pass^k underneath. Click a cell to open the run behind it.
            </CardDescription>
          </div>
          <div className="w-48">
            <Select value={sort} onChange={(event) => setSort(event.target.value as SortKey)} aria-label="Sort models">
              <option value="score">Sort by overall score</option>
              <option value="cost">Sort by cost</option>
              <option value="latency">Sort by latency</option>
              <option value="name">Sort by name</option>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full border-separate border-spacing-1">
              <thead>
                <tr>
                  <th className="sticky left-0 z-10 w-56 bg-card px-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    Model
                  </th>
                  {scenarios.map((scenario) => (
                    <th key={scenario.id} className="min-w-[9rem] px-2 pb-2 text-center">
                      <Link href={`/scenarios/${scenario.id}`} className="text-xs font-medium hover:underline">
                        {scenario.title ?? scenario.id}
                      </Link>
                      <div className="mt-1 flex justify-center">
                        <ConfigHashBadge hash={scenario.config_hash ?? null} changed={mixedScenarios.has(scenario.id)} />
                      </div>
                    </th>
                  ))}
                  <th className="min-w-[7rem] px-2 text-center text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    Overall
                  </th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((row) => (
                  <tr key={row.key}>
                    <th className="sticky left-0 z-10 bg-card px-2 py-1 text-left align-middle">
                      <div className="flex flex-col">
                        <span className="text-sm font-medium">{row.label}</span>
                        <span className="font-mono text-[11px] text-muted-foreground">{row.key}</span>
                      </div>
                    </th>
                    {scenarios.map((scenario) => {
                      const cell = row.cells.get(scenario.id);
                      const stale =
                        cell?.config_hash &&
                        referenceHash.get(scenario.id) &&
                        cell.config_hash !== referenceHash.get(scenario.id);
                      return (
                        <td key={scenario.id} className="p-0">
                          <HeatCell cell={cell} stale={Boolean(stale)} />
                        </td>
                      );
                    })}
                    <td className="p-0">
                      <div className="flex h-16 flex-col items-center justify-center rounded-md border bg-card">
                        <span className="text-sm font-semibold tabular-nums">
                          {formatPercent(row.meanPassAt1, 0)}
                          {row.notSignificantVsLeader && row.key !== sorted[0]?.key ? (
                            <span
                              className="ml-1 cursor-help text-[10px] font-normal text-warn"
                              title={
                                "Not separable from the leader: this model's 95% Wilson confidence " +
                                "interval for pass@1 overlaps the top model's, so the two cannot be " +
                                "told apart at this sample size. Run a larger k to narrow the intervals. " +
                                "Overlap does not mean the models are equal, only that this run does " +
                                "not establish a difference."
                              }
                            >
                              not separable
                            </span>
                          ) : null}
                        </span>
                        {row.passAt1Low !== null && row.passAt1High !== null ? (
                          <span
                            className="text-[10px] tabular-nums text-muted-foreground"
                            title="95% Wilson confidence interval for pass@1"
                          >
                            95% CI {formatPercent(row.passAt1Low, 0)}–{formatPercent(row.passAt1High, 0)}
                          </span>
                        ) : null}
                        <span className="text-[11px] text-muted-foreground">
                          {formatCost(row.totalCost)} · {formatMs(row.meanLatency)}
                        </span>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Axis scores</CardTitle>
          <CardDescription>
            Mean per-axis score across scenarios: passed assertions ÷ assertions tagged with that axis.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="radar">
            <TabsList>
              <TabsTrigger value="radar">Radar</TabsTrigger>
              <TabsTrigger value="bars">Bars</TabsTrigger>
            </TabsList>
            <TabsContent value="radar">
              <AxisRadar series={axisSeries} />
            </TabsContent>
            <TabsContent value="bars">
              <AxisBars series={axisSeries} />
            </TabsContent>
          </Tabs>
          <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {AXES.map((axis: Axis) => (
              <div key={axis} className="rounded-md border p-2 text-xs">
                <p className="font-mono text-muted-foreground">{axis}</p>
                <p className="mt-1 font-medium tabular-nums">
                  {formatPercent(mean(sorted.map((row) => row.axisMeans[axis] ?? null).filter((v): v is number => v !== null)), 0)}
                </p>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function HeatCell({ cell, stale }: { cell: LeaderboardCell | undefined; stale: boolean }) {
  if (!cell) {
    return (
      <div className="flex h-16 items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground">
        no run
      </div>
    );
  }

  const body = (
    <div
      className={cn(
        "flex h-16 flex-col items-center justify-center rounded-md border transition-colors hover:ring-2 hover:ring-ring",
        cellTone(cell.pass_at_1),
        stale && "border-warn",
      )}
    >
      <span className="text-sm font-semibold tabular-nums">
        {formatPercent(cell.pass_at_1, 0)}
        {cell.incomplete ? <span className="text-warn" title="incomplete run">*</span> : null}
      </span>
      <span className="text-[11px] text-muted-foreground">
        pass^{cell.k ?? "k"} {formatPercent(cell.pass_hat_k, 0)}
      </span>
      {stale ? <span className="text-[10px] font-semibold text-warn">config changed</span> : null}
    </div>
  );

  const wrapped = cell.run_id ? (
    <Link href={`/runs/${cell.run_id}`} className="block">
      {body}
    </Link>
  ) : (
    body
  );

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div>{wrapped}</div>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <p className="font-medium">
            {cell.model_key} · {cell.scenario_id}
          </p>
          {cell.incomplete ? (
            <p className="text-warn">
              incomplete: {formatPercent(cell.coverage ?? 0, 0)} of attempts graded; pass^k
              withheld
            </p>
          ) : null}
          <p>pass@1 {formatPercent(cell.pass_at_1, 1)}</p>
          <p>
            pass^{cell.k ?? "k"} {formatPercent(cell.pass_hat_k, 1)}
          </p>
          <p>cost {formatCost(cell.cost_usd)}</p>
          <p>
            latency p50 {formatMs(cell.latency_p50_ms ?? null)} · p95 {formatMs(cell.latency_p95_ms ?? null)}
          </p>
          <p className="font-mono text-[11px]">config {shortHash(cell.config_hash)}</p>
          {stale ? <p className="text-warn">This cell was scored against a different scenario config.</p> : null}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
