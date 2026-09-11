import type * as React from "react";
import { AlertOctagon, AlertTriangle, CheckCircle2, CircleDashed, CircleSlash, Loader2, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { formatScore, prettyJson, shortHash } from "@/lib/format";
import type { Axis, AxisScores, AttemptStatus, RunStatus, Severity } from "@/lib/types";
import { cn } from "@/lib/utils";

/* -------------------------------------------------------------------------- */
/* Status                                                                      */
/* -------------------------------------------------------------------------- */

type Status = RunStatus | AttemptStatus;

const STATUS_LABEL: Record<Status, string> = {
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  errored: "Errored",
  cancelled: "Cancelled",
};

export function statusTone(status: Status, passed?: boolean | null): "pass" | "fail" | "warn" | "muted" | "secondary" {
  if (status === "running") return "warn";
  if (status === "queued") return "muted";
  if (status === "cancelled") return "secondary";
  // An errored attempt produced no verdict at all, so it reads as a warning
  // about missing data rather than as a failure by the model (DECISIONS D19).
  if (status === "errored") return "warn";
  if (status === "failed") return "fail";
  // completed: pass/fail comes from the attempt verdict when we have one.
  if (passed === true) return "pass";
  if (passed === false) return "fail";
  return "secondary";
}

export function StatusBadge({ status, passed }: { status: Status; passed?: boolean | null }) {
  const tone = statusTone(status, passed);
  const Icon =
    status === "running"
      ? Loader2
      : status === "queued"
        ? CircleDashed
        : status === "cancelled"
          ? CircleSlash
          : status === "errored"
            ? AlertTriangle
            : status === "failed"
              ? AlertOctagon
              : passed === false
                ? XCircle
                : CheckCircle2;

  return (
    <Badge variant={tone === "muted" ? "muted" : tone}>
      <Icon className={cn("h-3 w-3", status === "running" && "animate-spin")} />
      {STATUS_LABEL[status]}
      {status === "completed" && passed !== undefined && passed !== null
        ? ` · ${passed ? "passed" : "failed"}`
        : ""}
    </Badge>
  );
}

export function PassFailBadge({ passed, label }: { passed: boolean | null | undefined; label?: string }) {
  if (passed === null || passed === undefined) {
    return <Badge variant="muted">{label ?? "not graded"}</Badge>;
  }
  return (
    <Badge variant={passed ? "pass" : "fail"}>
      {passed ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {label ?? (passed ? "pass" : "fail")}
    </Badge>
  );
}

/**
 * An attempt's verdict, which only exists if the attempt was graded.
 *
 * An errored or cancelled attempt carries `passed = false` in the database
 * because the column is not nullable, but it never produced a verdict at all.
 * Rendering that as "fail" blames the model for a provider outage, which is the
 * exact confusion the errored status exists to prevent (DECISIONS D19).
 */
export function AttemptVerdictBadge({
  status,
  passed,
}: {
  status: AttemptStatus;
  passed: boolean | null | undefined;
}) {
  if (status === "errored") {
    return (
      <Badge variant="warn" title="No verdict: the attempt never completed.">
        <AlertTriangle className="h-3 w-3" />
        errored
      </Badge>
    );
  }
  if (status === "cancelled") {
    return <Badge variant="secondary">cancelled</Badge>;
  }
  if (status === "queued" || status === "running") {
    return <Badge variant="muted">{status}</Badge>;
  }
  return <PassFailBadge passed={passed ?? null} />;
}

/* -------------------------------------------------------------------------- */
/* Axes and severities                                                         */
/* -------------------------------------------------------------------------- */

export function AxisBadge({ axis }: { axis: Axis }) {
  return (
    <Badge variant="outline" className="font-mono text-[11px]">
      {axis}
    </Badge>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const variant = severity === "critical" ? "destructive" : severity === "required" ? "secondary" : "muted";
  return (
    <Badge variant={variant} className="font-mono text-[11px]">
      {severity}
    </Badge>
  );
}

export function AxisScoreList({ scores, className }: { scores: AxisScores | undefined; className?: string }) {
  const entries = Object.entries(scores ?? {}).filter(([, value]) => value !== null && value !== undefined);
  if (entries.length === 0) {
    return <p className={cn("text-sm text-muted-foreground", className)}>No axis scores recorded.</p>;
  }
  return (
    <dl className={cn("grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4", className)}>
      {entries.map(([axis, value]) => (
        <div key={axis} className="flex items-center justify-between gap-2">
          <dt className="font-mono text-xs text-muted-foreground">{axis}</dt>
          <dd className="tabular-nums">{formatScore(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

/* -------------------------------------------------------------------------- */
/* Config hash                                                                 */
/* -------------------------------------------------------------------------- */

export function ConfigHashBadge({
  hash,
  changed = false,
  title,
}: {
  hash: string | null | undefined;
  changed?: boolean;
  title?: string;
}) {
  return (
    <Badge
      variant={changed ? "warn" : "outline"}
      className="font-mono text-[11px]"
      title={title ?? (hash ? `config_hash ${hash}` : "config hash unknown")}
    >
      {changed ? "config changed · " : ""}
      {shortHash(hash)}
    </Badge>
  );
}

/* -------------------------------------------------------------------------- */
/* Values                                                                      */
/* -------------------------------------------------------------------------- */

export function JsonBlock({
  value,
  className,
  maxHeight = "max-h-80",
}: {
  value: unknown;
  className?: string;
  maxHeight?: string;
}) {
  return (
    <pre
      className={cn(
        "scrollbar-thin overflow-auto rounded-md border bg-muted/50 p-3 font-mono text-xs leading-relaxed",
        maxHeight,
        className,
      )}
    >
      {prettyJson(value)}
    </pre>
  );
}

export function Stat({
  label,
  value,
  hint,
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  className?: string;
}) {
  return (
    <div className={cn("rounded-lg border bg-card p-3", className)}>
      <p className="text-xs uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold tabular-nums">{value}</p>
      {hint ? <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

export function KeyValue({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium">{children}</span>
    </div>
  );
}
