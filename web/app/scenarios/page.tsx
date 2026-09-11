import type { Metadata } from "next";
import Link from "next/link";
import { CheckCircle2, FileWarning } from "lucide-react";

import { ConfigHashBadge } from "@/components/domain";
import { ApiErrorState, EmptyState, PageHeading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { getScenarios } from "@/lib/api";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Scenarios" };

export default async function ScenariosPage() {
  const result = await getScenarios();

  return (
    <>
      <PageHeading
        title="Scenarios"
        description="Read-only view of the scenario definitions in git. Scenarios are authored as files; this dashboard never edits them."
      />

      {!result.ok ? (
        <ApiErrorState error={result.error} what="the scenario list" />
      ) : result.data.length === 0 ? (
        <EmptyState
          title="No scenarios found"
          description="Add a directory under scenarios/ with scenario.yaml, tools.json and fixtures/, then reload."
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {result.data.map((scenario) => (
            <Card key={scenario.id} className={scenario.valid ? undefined : "border-destructive/40"}>
              <CardContent className="space-y-3 p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <Link href={`/scenarios/${scenario.id}`} className="text-base font-semibold hover:underline">
                      {scenario.title}
                    </Link>
                    <p className="font-mono text-xs text-muted-foreground">{scenario.id}</p>
                  </div>
                  <Badge variant={scenario.valid ? "pass" : "destructive"}>
                    {scenario.valid ? <CheckCircle2 className="h-3 w-3" /> : <FileWarning className="h-3 w-3" />}
                    {scenario.valid ? "valid" : "invalid"}
                  </Badge>
                </div>

                {scenario.description ? (
                  <p className="line-clamp-3 text-sm text-muted-foreground">{scenario.description}</p>
                ) : null}

                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="muted">{scenario.turn_count} turns</Badge>
                  {scenario.version !== undefined ? <Badge variant="muted">v{scenario.version}</Badge> : null}
                  <ConfigHashBadge hash={scenario.config_hash} />
                  {(scenario.axes ?? []).map((axis) => (
                    <Badge key={axis} variant="outline" className="font-mono text-[11px]">
                      {axis}
                    </Badge>
                  ))}
                </div>

                {!scenario.valid && scenario.error ? (
                  <pre className="scrollbar-thin max-h-32 overflow-auto rounded-md border border-destructive/40 bg-destructive/5 p-2 font-mono text-xs text-destructive dark:text-fail">
                    {scenario.error}
                  </pre>
                ) : null}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
