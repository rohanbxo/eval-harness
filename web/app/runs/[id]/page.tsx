import type { Metadata } from "next";
import Link from "next/link";

import { RunLiveView } from "@/components/runs/run-live-view";
import { ApiErrorState, PageHeading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { getRun } from "@/lib/api";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Run" };

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await getRun(id);

  return (
    <>
      <PageHeading
        title="Run"
        description="Live progress, per-attempt status, and the stored summary once every attempt has finished."
        actions={
          <Button variant="outline" size="sm" asChild>
            <Link href="/runs">All runs</Link>
          </Button>
        }
      />

      {result.ok ? <RunLiveView initialRun={result.data} /> : <ApiErrorState error={result.error} what="this run" />}
    </>
  );
}
