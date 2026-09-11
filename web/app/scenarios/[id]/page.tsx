import type { Metadata } from "next";
import Link from "next/link";
import { CheckCircle2, FileWarning } from "lucide-react";

import { AxisBadge, ConfigHashBadge, JsonBlock, SeverityBadge } from "@/components/domain";
import { ApiErrorState, PageHeading } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { getScenario } from "@/lib/api";
import { formatDateTime, prettyJson } from "@/lib/format";
import type { ScenarioDetail, ScenarioTurn } from "@/lib/types";

/** One assertion, as the generated schema models it (a discriminated union). */
type ScenarioAssertion = NonNullable<ScenarioTurn["assertions"]>[number];

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Scenario" };

/** Fields already shown as badges; the rest is the assertion's real content. */
const COMMON_KEYS = new Set(["id", "type", "axis", "severity", "scope"]);

function assertionBody(assertion: ScenarioAssertion): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(assertion)) {
    if (!COMMON_KEYS.has(key) && value !== undefined) {
      body[key] = value;
    }
  }
  return body;
}

export default async function ScenarioPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await getScenario(id);

  if (!result.ok) {
    return (
      <>
        <PageHeading title="Scenario" description={id} />
        <ApiErrorState error={result.error} what="this scenario" />
      </>
    );
  }

  const detail: ScenarioDetail = result.data;
  const definition = detail.scenario;

  if (!definition) {
    // The scenario exists on disk but failed to load. The validation error is
    // the only thing worth showing, and it is what the author needs.
    return (
      <>
        <PageHeading
          title={detail.id}
          description="This scenario could not be loaded."
          actions={
            <Button variant="outline" size="sm" asChild>
              <Link href="/scenarios">All scenarios</Link>
            </Button>
          }
        />
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="p-5">
            <p className="mb-2 font-medium text-destructive dark:text-fail">Validation failed</p>
            <pre className="scrollbar-thin max-h-64 overflow-auto whitespace-pre-wrap font-mono text-xs">
              {detail.error ?? "No details were reported."}
            </pre>
          </CardContent>
        </Card>
      </>
    );
  }

  const scenario = definition;
  const turns = scenario.turns ?? [];
  const tools = detail.tools ?? [];
  const fixtures = Object.entries(detail.fixtures ?? {});

  return (
    <>
      <PageHeading
        title={scenario.title}
        description={scenario.description}
        actions={
          <Button variant="outline" size="sm" asChild>
            <Link href="/scenarios">All scenarios</Link>
          </Button>
        }
      />

      <div className="mb-6 flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="font-mono text-[11px]">
          {detail.id}
        </Badge>
        <Badge variant={detail.valid ? "pass" : "destructive"}>
          {detail.valid ? <CheckCircle2 className="h-3 w-3" /> : <FileWarning className="h-3 w-3" />}
          {detail.valid ? "valid" : "invalid"}
        </Badge>
        <Badge variant="muted">v{scenario.version}</Badge>
        <Badge variant="muted">{turns.length} turns</Badge>
        <ConfigHashBadge hash={detail.config_hash} />
        {(scenario.axes ?? []).map((axis) => (
          <AxisBadge key={axis} axis={axis} />
        ))}
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Turns</CardTitle>
              <CardDescription>The scripted conversation and the assertions graded after each turn.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              {turns.map((turn, index) => (
                <div key={index} className="space-y-3">
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary">Turn {index + 1}</Badge>
                  </div>
                  <p className="whitespace-pre-wrap rounded-md border-l-4 border-l-primary/60 bg-muted/40 p-3 text-sm leading-relaxed">
                    {turn.user}
                  </p>
                  {(turn.assertions ?? []).length === 0 ? (
                    <p className="text-xs text-muted-foreground">No assertions on this turn.</p>
                  ) : (
                    <div className="space-y-2">
                      {(turn.assertions ?? []).map((assertion) => (
                        <div key={assertion.id} className="rounded-md border p-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <code className="font-mono text-xs font-semibold">{assertion.id}</code>
                            <Badge variant="muted" className="font-mono text-[11px]">
                              {assertion.type}
                            </Badge>
                            <AxisBadge axis={assertion.axis} />
                            <SeverityBadge severity={assertion.severity} />
                            {assertion.scope === "scenario" ? <Badge variant="warn">scenario scope</Badge> : null}
                          </div>
                          <JsonBlock value={assertionBody(assertion)} className="mt-2" maxHeight="max-h-56" />
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Tools</CardTitle>
              <CardDescription>
                OpenAI function-calling schemas. The <code className="font-mono">mock</code> block is stripped before
                tools reach the model.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {tools.length === 0 ? (
                <p className="text-sm text-muted-foreground">This scenario declares no tools.</p>
              ) : (
                tools.map((tool) => (
                  <details key={tool.name} className="rounded-md border p-3">
                    <summary className="cursor-pointer">
                      <span className="font-mono text-sm font-semibold">{tool.name}</span>
                      {tool.mock ? (
                        <Badge variant="muted" className="ml-2">
                          mock: {tool.mock.kind === "fixture" ? tool.mock.file : tool.mock.name}
                        </Badge>
                      ) : null}
                    </summary>
                    <p className="mt-2 text-sm text-muted-foreground">{tool.description}</p>
                    <JsonBlock value={tool.parameters} className="mt-2" />
                  </details>
                ))
              )}
            </CardContent>
          </Card>

          {fixtures.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>Fixtures</CardTitle>
                <CardDescription>Mock tool responses, matched top-down (SPEC 5.2).</CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                {fixtures.map(([path, content]) => (
                  <details key={path} className="rounded-md border p-3">
                    <summary className="cursor-pointer font-mono text-sm">{path}</summary>
                    <JsonBlock value={content} className="mt-2" />
                  </details>
                ))}
              </CardContent>
            </Card>
          ) : null}
        </div>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>System prompt</CardTitle>
              <CardDescription>
                The harness prepends one line with the frozen clock. Nothing else is added.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="scrollbar-thin max-h-80 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/40 p-3 text-xs leading-relaxed">
                {scenario.system_prompt}
              </pre>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Configuration</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <Row label="Clock">{formatDateTime(scenario.clock)}</Row>
              <Row label="Max steps / turn">{scenario.limits?.max_steps_per_turn ?? "default"}</Row>
              <Row label="Turn timeout">{scenario.limits ? `${scenario.limits.turn_timeout_s}s` : "default"}</Row>
              <Row label="Continue on fail">{scenario.continue_on_fail === false ? "no" : "yes"}</Row>
              <Row label="config_hash">
                <code className="break-all font-mono text-xs">{detail.config_hash}</code>
              </Row>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Faults</CardTitle>
              <CardDescription>Scripted tool failures injected during the attempt (SPEC 5.4).</CardDescription>
            </CardHeader>
            <CardContent>
              {!scenario.faults || scenario.faults.length === 0 ? (
                <p className="text-sm text-muted-foreground">No faults are injected in this scenario.</p>
              ) : (
                <ul className="space-y-2">
                  {scenario.faults.map((fault, index) => (
                    <li key={`${fault.tool}-${fault.on_call}-${index}`} className="rounded-md border p-3 text-sm">
                      <p>
                        <code className="font-mono font-semibold">{fault.tool}</code> · call #{fault.on_call}
                      </p>
                      <pre className="scrollbar-thin mt-2 max-h-32 overflow-auto rounded bg-muted/50 p-2 font-mono text-xs">
                        {prettyJson(fault.response)}
                      </pre>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}
