"use client";

import * as React from "react";
import { ChevronRight, CircleSlash, FlaskConical } from "lucide-react";

import { AxisBadge, JsonBlock, PassFailBadge, SeverityBadge } from "@/components/domain";
import { Badge } from "@/components/ui/badge";
import type { AssertionResult } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * One graded assertion. The matcher failure reason is the point of this row -
 * it is shown inline, verbatim, never truncated behind a click.
 */
export function AssertionRow({ assertion, showRaw }: { assertion: AssertionResult; showRaw: boolean }) {
  const [open, setOpen] = React.useState(false);
  const hasDetails = assertion.details !== undefined && Object.keys(assertion.details).length > 0;

  return (
    <div
      className={cn(
        "rounded-md border px-3 py-2 text-sm",
        // A not-evaluable assertion decided nothing, so it must not read as a
        // green pass: it is styled neutrally and badged (D38).
        assertion.evaluable === false
          ? "border-muted bg-muted/30"
          : assertion.passed
            ? "border-pass/40 bg-pass/5"
            : "border-fail/50 bg-fail/5",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        {assertion.evaluable === false ? (
          <Badge variant="muted" title="The attempt produced no evidence either way.">
            <CircleSlash className="h-3 w-3" />
            not evaluable
          </Badge>
        ) : (
          <PassFailBadge passed={assertion.passed} />
        )}
        <code className="font-mono text-xs font-semibold">{assertion.assertion_id}</code>
        <Badge variant="muted" className="font-mono text-[11px]">
          {assertion.type}
        </Badge>
        <AxisBadge axis={assertion.axis} />
        <SeverityBadge severity={assertion.severity} />
        {assertion.non_deterministic ? (
          <Badge variant="warn" title="Judged by an LLM; not deterministic.">
            <FlaskConical className="h-3 w-3" />
            non-deterministic
          </Badge>
        ) : null}
      </div>

      {assertion.reason ? (
        <p
          className={cn(
            "mt-2 whitespace-pre-wrap break-words font-mono text-xs leading-relaxed",
            assertion.passed || assertion.evaluable === false ? "text-muted-foreground" : "text-fail",
          )}
        >
          {assertion.reason}
        </p>
      ) : null}

      {hasDetails ? (
        <div className="mt-2">
          {showRaw ? (
            <JsonBlock value={assertion.details} maxHeight="max-h-64" />
          ) : (
            <>
              <button
                type="button"
                onClick={() => setOpen((value) => !value)}
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                aria-expanded={open}
              >
                <ChevronRight className={cn("h-3 w-3 transition-transform", open && "rotate-90")} />
                {open ? "Hide" : "Show"} matcher details
              </button>
              {open ? <JsonBlock value={assertion.details} className="mt-2" maxHeight="max-h-64" /> : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function AssertionGroup({
  title,
  assertions,
  showRaw,
  emptyLabel,
}: {
  title: string;
  assertions: AssertionResult[];
  showRaw: boolean;
  emptyLabel?: string;
}) {
  if (assertions.length === 0) {
    return emptyLabel ? (
      <div className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">{emptyLabel}</div>
    ) : null;
  }
  return (
    <div className="space-y-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
      {assertions.map((assertion) => (
        <AssertionRow key={`${assertion.assertion_id}-${assertion.turn_index ?? "scenario"}`} assertion={assertion} showRaw={showRaw} />
      ))}
    </div>
  );
}
