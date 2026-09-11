import type * as React from "react";
import { AlertTriangle, Inbox, PlugZap } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

export function PageHeading({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon,
  className,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  icon?: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("border-dashed", className)}>
      <CardContent className="flex flex-col items-center gap-3 p-10 text-center">
        <div className="rounded-full bg-muted p-3 text-muted-foreground">{icon ?? <Inbox className="h-5 w-5" />}</div>
        <div>
          <p className="font-medium">{title}</p>
          {description ? <p className="mt-1 text-sm text-muted-foreground">{description}</p> : null}
        </div>
        {action}
      </CardContent>
    </Card>
  );
}

/**
 * Rendered whenever a server fetch fails. A failed fetch is expected during
 * local development (the API may simply not be running), so it reads as a
 * status message rather than a crash.
 */
export function ApiErrorState({
  error,
  what,
  className,
}: {
  error: ApiError;
  what: string;
  className?: string;
}) {
  const unreachable = error.unreachable;
  return (
    <Card className={cn("border-destructive/40 bg-destructive/5", className)}>
      <CardContent className="flex flex-col items-start gap-3 p-6">
        <div className="flex items-center gap-2 text-destructive dark:text-fail">
          {unreachable ? <PlugZap className="h-5 w-5" /> : <AlertTriangle className="h-5 w-5" />}
          <p className="font-medium">
            {unreachable ? `Could not reach the API to load ${what}.` : `The API returned an error loading ${what}.`}
          </p>
        </div>
        <p className="text-sm text-muted-foreground">
          {unreachable
            ? "Start the backend with `docker compose up api worker` and reload. Nothing is cached locally, so this page will fill in as soon as the API answers."
            : `HTTP ${error.status}: ${error.message}`}
        </p>
        {unreachable ? <code className="rounded bg-muted px-2 py-1 font-mono text-xs">{error.message}</code> : null}
      </CardContent>
    </Card>
  );
}

export function InlineError({
  message,
  className,
  tone = "error",
}: {
  message: string;
  className?: string;
  /** "warn" for missing data the user should weigh, "error" for a failure. */
  tone?: "error" | "warn";
}) {
  return (
    <p
      className={cn(
        "flex items-start gap-2 text-sm",
        tone === "warn" ? "text-warn" : "text-destructive dark:text-fail",
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message}</span>
    </p>
  );
}

export function TableSkeleton({ rows = 5, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div key={rowIndex} className="flex gap-3">
          {Array.from({ length: columns }).map((__, columnIndex) => (
            <Skeleton key={columnIndex} className="h-9 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}

export function CardSkeleton({ className }: { className?: string }) {
  return (
    <Card className={className}>
      <CardContent className="space-y-3 p-5">
        <Skeleton className="h-5 w-1/3" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-24 w-full" />
      </CardContent>
    </Card>
  );
}
