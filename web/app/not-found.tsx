import Link from "next/link";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/states";

export default function NotFound() {
  return (
    <EmptyState
      title="Page not found"
      description="That route does not exist in the dashboard."
      action={
        <Button size="sm" asChild>
          <Link href="/">Back to the leaderboard</Link>
        </Button>
      }
    />
  );
}
