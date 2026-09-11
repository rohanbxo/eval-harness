"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { KeyRound, Loader2, Rocket } from "lucide-react";

import { ConfigHashBadge } from "@/components/domain";
import { InlineError } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { postRun } from "@/lib/client-api";
import type { CreateRunRequest, JsonObject, ModelEntry, ScenarioListItem } from "@/lib/types";
import { cn } from "@/lib/utils";

interface Props {
  models: ModelEntry[];
  scenarios: ScenarioListItem[];
}

function isRunnable(model: ModelEntry): boolean {
  return model.api_key_present && model.supports_tool_calling !== false;
}

function disabledReason(model: ModelEntry): string {
  if (model.supports_tool_calling === false) {
    return `${model.display_name} does not support native tool calling, so the harness cannot run it (SPEC 1).`;
  }
  return model.api_key_env
    ? `No API key found. Set ${model.api_key_env} in .env and restart the API container.`
    : "No API key found for this provider. Set the provider's key in .env and restart the API container.";
}

export function RunLauncherForm({ models, scenarios }: Props) {
  const router = useRouter();

  const firstRunnable = models.find(isRunnable);
  const [modelKey, setModelKey] = React.useState<string>(firstRunnable?.key ?? "");
  const [selected, setSelected] = React.useState<string[]>(() => scenarios.filter((s) => s.valid).map((s) => s.id));
  const [k, setK] = React.useState<number>(3);
  const [overridesText, setOverridesText] = React.useState<string>("");
  const [overridesError, setOverridesError] = React.useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (body: CreateRunRequest) => postRun(body),
    onSuccess: (run) => {
      router.push(`/runs/${run.id}`);
    },
  });

  const selectedModel = models.find((model) => model.key === modelKey) ?? null;
  const validScenarios = scenarios.filter((scenario) => scenario.valid);
  const canSubmit =
    selectedModel !== null && isRunnable(selectedModel) && selected.length > 0 && k >= 1 && !mutation.isPending;

  function toggleScenario(id: string, checked: boolean) {
    setSelected((current) => (checked ? [...new Set([...current, id])] : current.filter((value) => value !== id)));
  }

  function parseOverrides(): JsonObject | null | "invalid" {
    const text = overridesText.trim();
    if (!text) return null;
    try {
      const parsed: unknown = JSON.parse(text);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return "invalid";
      }
      return parsed as JsonObject;
    } catch {
      return "invalid";
    }
  }

  function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const overrides = parseOverrides();
    if (overrides === "invalid") {
      setOverridesError("Parameter overrides must be a JSON object, for example {\"temperature\": 0}.");
      return;
    }
    setOverridesError(null);
    if (!selectedModel) return;
    mutation.mutate({
      model_key: selectedModel.key,
      scenario_ids: selected,
      k,
      params_override: overrides,
      // A run launched from the dashboard is a comparison run: hold it to the
      // clean-tree guard (D30). The API returns 409 with an explanation, which
      // the form surfaces, rather than recording a commit that is not the code
      // that ran.
      allow_dirty: false,
    });
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Model</CardTitle>
            <CardDescription>
              Models come from <code className="font-mono">config/models.yaml</code>. A model without its API key
              present cannot be selected.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {models.length === 0 ? (
              <p className="text-sm text-muted-foreground">The registry is empty. Add an entry to config/models.yaml.</p>
            ) : (
              models.map((model) => {
                const runnable = isRunnable(model);
                const row = (
                  <label
                    key={model.key}
                    className={cn(
                      "flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors",
                      !runnable && "cursor-not-allowed opacity-60",
                      modelKey === model.key && runnable && "border-ring bg-accent/40",
                    )}
                  >
                    <input
                      type="radio"
                      name="model"
                      className="mt-1 accent-[hsl(var(--primary))]"
                      value={model.key}
                      checked={modelKey === model.key}
                      disabled={!runnable}
                      onChange={() => setModelKey(model.key)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{model.display_name}</span>
                        <Badge variant="outline" className="font-mono text-[11px]">
                          {model.key}
                        </Badge>
                        {model.supports_parallel_tool_calls ? <Badge variant="muted">parallel tool calls</Badge> : null}
                        {!runnable ? (
                          <Badge variant="warn">
                            <KeyRound className="h-3 w-3" />
                            {model.supports_tool_calling === false ? "no tool calling" : "API key missing"}
                          </Badge>
                        ) : null}
                      </span>
                      <span className="mt-1 block font-mono text-xs text-muted-foreground">{model.litellm_model}</span>
                    </span>
                  </label>
                );

                return runnable ? (
                  row
                ) : (
                  <Tooltip key={model.key}>
                    {/* Disabled inputs do not fire pointer events, so the tooltip
                        wraps the row rather than the input itself. */}
                    <TooltipTrigger asChild>
                      <div tabIndex={0}>{row}</div>
                    </TooltipTrigger>
                    <TooltipContent>{disabledReason(model)}</TooltipContent>
                  </Tooltip>
                );
              })
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Scenarios</CardTitle>
            <CardDescription>
              {validScenarios.length} of {scenarios.length} scenarios load cleanly. Invalid scenarios cannot be run
              until <code className="font-mono">evalharness validate</code> passes.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {scenarios.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No scenarios found. Add one under <code className="font-mono">scenarios/</code>.
              </p>
            ) : (
              <>
                <div className="flex gap-2 pb-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setSelected(validScenarios.map((scenario) => scenario.id))}
                  >
                    Select all valid
                  </Button>
                  <Button type="button" variant="ghost" size="sm" onClick={() => setSelected([])}>
                    Clear
                  </Button>
                </div>
                {scenarios.map((scenario) => (
                  <div
                    key={scenario.id}
                    className={cn(
                      "flex items-start gap-3 rounded-md border p-3",
                      !scenario.valid && "border-destructive/40 bg-destructive/5",
                    )}
                  >
                    <Checkbox
                      id={`scenario-${scenario.id}`}
                      className="mt-1"
                      checked={selected.includes(scenario.id)}
                      disabled={!scenario.valid}
                      onCheckedChange={(checked) => toggleScenario(scenario.id, checked === true)}
                    />
                    <div className="min-w-0 flex-1">
                      <Label htmlFor={`scenario-${scenario.id}`} className="cursor-pointer">
                        {scenario.title}
                      </Label>
                      <div className="mt-1 flex flex-wrap items-center gap-2">
                        <Badge variant="outline" className="font-mono text-[11px]">
                          {scenario.id}
                        </Badge>
                        <Badge variant="muted">{scenario.turn_count} turns</Badge>
                        <ConfigHashBadge hash={scenario.config_hash} />
                        {(scenario.axes ?? []).map((axis) => (
                          <Badge key={axis} variant="secondary" className="font-mono text-[11px]">
                            {axis}
                          </Badge>
                        ))}
                      </div>
                      {!scenario.valid ? (
                        <InlineError className="mt-2" message={scenario.error ?? "This scenario failed validation."} />
                      ) : null}
                    </div>
                  </div>
                ))}
              </>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="k">Repetitions (k)</Label>
              <Input
                id="k"
                type="number"
                min={1}
                max={20}
                value={k}
                onChange={(event) => setK(Math.max(1, Math.min(20, Number(event.target.value) || 1)))}
              />
              <p className="text-xs text-muted-foreground">
                Each scenario runs k times. pass^k is the fraction of scenarios where all k attempts passed.
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="overrides">Parameter overrides (optional)</Label>
              <Textarea
                id="overrides"
                placeholder={'{ "temperature": 0 }'}
                value={overridesText}
                onChange={(event) => setOverridesText(event.target.value)}
                rows={5}
              />
              <p className="text-xs text-muted-foreground">
                Merged over the registry entry&apos;s <code className="font-mono">params</code> for this run only.
              </p>
              {overridesError ? <InlineError message={overridesError} /> : null}
            </div>

            <div className="rounded-md border bg-muted/40 p-3 text-sm">
              <p className="font-medium">
                {selected.length} scenario{selected.length === 1 ? "" : "s"} × {k} = {selected.length * k} attempts
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {selectedModel ? selectedModel.display_name : "No model selected"}
              </p>
            </div>

            {mutation.isError ? (
              <InlineError message={mutation.error instanceof Error ? mutation.error.message : "Could not launch the run."} />
            ) : null}

            <Button type="submit" className="w-full" disabled={!canSubmit}>
              {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Rocket className="h-4 w-4" />}
              {mutation.isPending ? "Launching…" : "Launch run"}
            </Button>
            {selected.length === 0 ? (
              <p className="text-center text-xs text-muted-foreground">Select at least one scenario.</p>
            ) : null}
            {selectedModel === null ? (
              <p className="text-center text-xs text-muted-foreground">Select a model with its API key present.</p>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </form>
  );
}
