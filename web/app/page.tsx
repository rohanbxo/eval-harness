import Link from "next/link";
import { Rocket } from "lucide-react";

import { LeaderboardView } from "@/components/leaderboard/leaderboard-view";
import { ApiErrorState, PageHeading } from "@/components/states";
import { Button } from "@/components/ui/button";
import { ReadOnlyNote } from "@/components/read-only-note";
import { getLeaderboard } from "@/lib/api";
import { READ_ONLY } from "@/lib/read-only";

export const dynamic = "force-dynamic";

export default async function LeaderboardPage() {
  const result = await getLeaderboard();

  return (
    <>
      <PageHeading
        title="Leaderboard"
        description="The latest completed run per model per scenario config hash. Results from different config hashes are never mixed silently."
        actions={
          READ_ONLY ? null : (
            <Button size="sm" asChild>
              <Link href="/runs/new">
                <Rocket className="h-4 w-4" />
                New run
              </Link>
            </Button>
          )
        }
      />

      <ReadOnlyNote withFeaturedLink />

      {result.ok ? (
        <LeaderboardView data={result.data} />
      ) : (
        <ApiErrorState error={result.error} what="the leaderboard" />
      )}
    </>
  );
}
