import type { Metadata } from "next";
import Link from "next/link";
import { Rocket } from "lucide-react";

import { ConfigHashBadge, StatusBadge } from "@/components/domain";
import { ApiErrorState, EmptyState, PageHeading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { getRuns } from "@/lib/api";
import { formatCost, formatDateTime, formatPercent } from "@/lib/format";
import { READ_ONLY } from "@/lib/read-only";
import { RUN_STATUSES, type RunStatus } from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Runs" };

function asStatus(value: string | undefined): RunStatus | undefined {
  return value && (RUN_STATUSES as readonly string[]).includes(value) ? (value as RunStatus) : undefined;
}

/** The API paginates by limit/offset; the pager below thinks in 1-based pages. */
function pageOf(data: { limit: number; offset: number }): number {
  return data.limit > 0 ? Math.floor(data.offset / data.limit) + 1 : 1;
}

export default async function RunsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const model = typeof params.model === "string" && params.model ? params.model : undefined;
  const status = asStatus(typeof params.status === "string" ? params.status : undefined);
  const page = typeof params.page === "string" ? Number(params.page) || 1 : 1;

  const result = await getRuns({ model, status, page });

  return (
    <>
      <PageHeading
        title="Runs"
        description="Every run ever launched, newest first. Runs are never mutated; the database is the record."
        actions={
          READ_ONLY ? null : (
            <Button size="sm" asChild>
              <Link href="/runs/new">
                <Rocket className="h-4 w-4" />
                New run
              </Link>
            </Button>
          )
        }
      />

      {/* A plain GET form: filtering needs no client-side JavaScript. */}
      <form method="get" action="/runs" className="mb-4 flex flex-wrap items-end gap-3">
        <div className="w-48">
          <label htmlFor="model" className="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">
            Model key
          </label>
          <input
            id="model"
            name="model"
            defaultValue={model ?? ""}
            placeholder="any"
            className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <div className="w-44">
          <label htmlFor="status" className="mb-1 block text-xs uppercase tracking-wide text-muted-foreground">
            Status
          </label>
          <Select id="status" name="status" defaultValue={status ?? ""}>
            <option value="">Any</option>
            {RUN_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </Select>
        </div>
        <Button type="submit" variant="outline" size="sm">
          Apply
        </Button>
        {model || status ? (
          <Button type="button" variant="ghost" size="sm" asChild>
            <Link href="/runs">Clear</Link>
          </Button>
        ) : null}
      </form>

      {!result.ok ? (
        <ApiErrorState error={result.error} what="the run list" />
      ) : result.data.items.length === 0 ? (
        <EmptyState
          title={model || status ? "No runs match this filter" : "No runs yet"}
          description={
            model || status
              ? "Try clearing the filter."
              : READ_ONLY
                ? "This demo serves a recorded run, so nothing new appears here."
                : "Launch a run to start collecting traces. The FakeModel entry needs no API key."
          }
          action={
            READ_ONLY ? undefined : (
              <Button size="sm" asChild>
                <Link href="/runs/new">Launch the first run</Link>
              </Button>
            )
          }
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Scenarios</TableHead>
                  <TableHead className="text-right">k</TableHead>
                  <TableHead className="text-right">pass@1</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {result.data.items.map((run) => (
                  <TableRow key={run.id}>
                    <TableCell>
                      <Link href={`/runs/${run.id}`} className="font-mono text-xs underline-offset-4 hover:underline">
                        {run.id.slice(0, 12)}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{run.model_key ?? run.model_key}</Badge>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={run.status} />
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {(run.scenario_ids ?? []).slice(0, 3).map((scenarioId) => (
                          <Badge key={scenarioId} variant="outline" className="font-mono text-[11px]">
                            {scenarioId}
                          </Badge>
                        ))}
                        {(run.scenario_ids ?? []).length > 3 ? (
                          <Badge variant="muted">+{(run.scenario_ids ?? []).length - 3}</Badge>
                        ) : null}
                        {run.config_hashes && Object.keys(run.config_hashes).length > 0 ? (
                          <ConfigHashBadge hash={Object.values(run.config_hashes)[0] ?? null} />
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{run.k}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatPercent(run.summary?.pass_at_1 ?? null, 1)}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{formatCost(run.summary?.cost_usd ?? null)}</TableCell>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatDateTime(run.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {result.ok && result.data.total > result.data.items.length ? (
        <div className="mt-4 flex items-center justify-between text-sm text-muted-foreground">
          <span>
            Page {pageOf(result.data)} · {result.data.total} runs total
          </span>
          <div className="flex gap-2">
            {pageOf(result.data) > 1 ? (
              <Button variant="outline" size="sm" asChild>
                <Link href={{ pathname: "/runs", query: { ...(model ? { model } : {}), ...(status ? { status } : {}), page: pageOf(result.data) - 1 } }}>
                  Previous
                </Link>
              </Button>
            ) : null}
            <Button variant="outline" size="sm" asChild>
              <Link href={{ pathname: "/runs", query: { ...(model ? { model } : {}), ...(status ? { status } : {}), page: pageOf(result.data) + 1 } }}>
                Next
              </Link>
            </Button>
          </div>
        </div>
      ) : null}
    </>
  );
}
