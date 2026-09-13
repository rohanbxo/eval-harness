import type { Metadata } from "next";
import Link from "next/link";

import { ReadOnlyNote } from "@/components/read-only-note";
import { RunLauncherForm } from "@/components/runs/run-launcher-form";
import { ApiErrorState, PageHeading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { getModels, getScenarios } from "@/lib/api";
import { READ_ONLY } from "@/lib/read-only";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "New run" };

export default async function NewRunPage() {
  // In read-only mode the launcher is not rendered at all. Fetching the registry
  // first would be wasted work, and the API refuses the launch anyway (405).
  if (READ_ONLY) {
    return (
      <>
        <PageHeading
          title="Launching runs is disabled here"
          description="The hosted dashboard is a read-only view over a recorded run."
        />
        <ReadOnlyNote />
        <Button size="sm" asChild>
          <Link href="/">Back to the leaderboard</Link>
        </Button>
      </>
    );
  }

  const [models, scenarios] = await Promise.all([getModels(), getScenarios()]);

  return (
    <>
      <PageHeading
        title="Launch a run"
        description="Pick a model and the scenarios to run against it. Every attempt is persisted with a snapshot of the exact scenario config used."
      />

      {!models.ok ? (
        <ApiErrorState error={models.error} what="the model registry" className="mb-4" />
      ) : null}
      {!scenarios.ok ? <ApiErrorState error={scenarios.error} what="the scenario list" /> : null}

      {models.ok && scenarios.ok ? <RunLauncherForm models={models.data} scenarios={scenarios.data} /> : null}
    </>
  );
}
