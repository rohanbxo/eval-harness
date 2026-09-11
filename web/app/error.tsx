"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  React.useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <Card className="border-destructive/40 bg-destructive/5">
      <CardContent className="space-y-3 p-6">
        <p className="flex items-center gap-2 font-medium text-destructive dark:text-fail">
          <AlertTriangle className="h-5 w-5" />
          Something went wrong rendering this page.
        </p>
        <p className="font-mono text-xs text-muted-foreground">{error.message}</p>
        <Button variant="outline" size="sm" onClick={reset}>
          Try again
        </Button>
      </CardContent>
    </Card>
  );
}
