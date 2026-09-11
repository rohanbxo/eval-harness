"use client";

import * as React from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AXES, type Axis, type AxisScores } from "@/lib/types";

export interface AxisSeries {
  name: string;
  scores: AxisScores;
}

/** Deterministic, theme-safe series colours. */
const SERIES_COLORS = [
  "hsl(217 91% 60%)",
  "hsl(160 84% 39%)",
  "hsl(38 92% 50%)",
  "hsl(280 65% 60%)",
  "hsl(0 72% 55%)",
  "hsl(190 90% 42%)",
] as const;

function toRows(series: AxisSeries[]): Array<Record<string, string | number>> {
  return AXES.map((axis: Axis) => {
    const row: Record<string, string | number> = { axis };
    for (const entry of series) {
      const value = entry.scores[axis];
      row[entry.name] = value ?? 0;
    }
    return row;
  });
}

function useMounted(): boolean {
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);
  return mounted;
}

export function AxisRadar({ series, height = 320 }: { series: AxisSeries[]; height?: number }) {
  const mounted = useMounted();
  const rows = React.useMemo(() => toRows(series), [series]);

  if (series.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">No axis scores to chart yet.</p>;
  }
  if (!mounted) {
    return <div style={{ height }} className="animate-pulse rounded-md bg-muted" />;
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <RadarChart data={rows} outerRadius="72%">
        <PolarGrid stroke="hsl(var(--border))" />
        <PolarAngleAxis dataKey="axis" tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }} />
        <PolarRadiusAxis domain={[0, 1]} tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 10 }} />
        {series.map((entry, index) => (
          <Radar
            key={entry.name}
            name={entry.name}
            dataKey={entry.name}
            stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
            fill={SERIES_COLORS[index % SERIES_COLORS.length]}
            fillOpacity={0.18}
          />
        ))}
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Tooltip
          contentStyle={{
            background: "hsl(var(--popover))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
            color: "hsl(var(--popover-foreground))",
            fontSize: 12,
          }}
        />
      </RadarChart>
    </ResponsiveContainer>
  );
}

export function AxisBars({ series, height = 320 }: { series: AxisSeries[]; height?: number }) {
  const mounted = useMounted();
  const rows = React.useMemo(() => toRows(series), [series]);

  if (series.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">No axis scores to chart yet.</p>;
  }
  if (!mounted) {
    return <div style={{ height }} className="animate-pulse rounded-md bg-muted" />;
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
        <XAxis dataKey="axis" tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }} interval={0} angle={-25} textAnchor="end" height={60} />
        <YAxis domain={[0, 1]} tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }} />
        <Tooltip
          cursor={{ fill: "hsl(var(--muted))", opacity: 0.4 }}
          contentStyle={{
            background: "hsl(var(--popover))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
            color: "hsl(var(--popover-foreground))",
            fontSize: 12,
          }}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {series.map((entry, index) => (
          <Bar key={entry.name} dataKey={entry.name} fill={SERIES_COLORS[index % SERIES_COLORS.length]} radius={[3, 3, 0, 0]} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}
