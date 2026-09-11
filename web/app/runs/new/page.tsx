import type { Metadata } from "next";

import { RunLauncherForm } from "@/components/runs/run-launcher-form";
import { ApiErrorState, PageHeading } from "@/components/states";
import { getModels, getScenarios } from "@/lib/api";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "New run" };

export default async function NewRunPage() {
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
