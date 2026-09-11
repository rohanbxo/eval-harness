import type { Metadata } from "next";

import { ApiErrorState, PageHeading } from "@/components/states";
import { TraceView } from "@/components/trace/trace-view";
import { getAttempt } from "@/lib/api";
import { buildTraceModel } from "@/lib/trace";

export const dynamic = "force-dynamic";

export const metadata: Metadata = { title: "Trace" };

export default async function AttemptPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const result = await getAttempt(id);

  if (!result.ok) {
    return (
      <>
        <PageHeading title="Trace" description={`Attempt ${id}`} />
        <ApiErrorState error={result.error} what="this attempt" />
      </>
    );
  }

  const trace = result.data;
  const model = buildTraceModel(trace);

  return <TraceView trace={trace} model={model} />;
}
