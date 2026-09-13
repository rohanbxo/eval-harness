import Link from "next/link";
import { ArrowRight, Info } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { FEATURED_ATTEMPT_ID, READ_ONLY, READ_ONLY_NOTE, WRITEUP_URL } from "@/lib/read-only";

/**
 * Explains what this deployment is, and points at the finding worth seeing.
 *
 * Renders nothing outside read-only mode, so a local dashboard is unchanged.
 * The note says what the demo *is* rather than only what it cannot do: a visitor
 * who lands here from a README needs the context more than the restriction.
 */
export function ReadOnlyNote({ withFeaturedLink = false }: { withFeaturedLink?: boolean }) {
  if (!READ_ONLY) return null;

  return (
    <Alert className="mb-6">
      <Info className="h-4 w-4" />
      <AlertDescription className="space-y-2">
        <p>
          {READ_ONLY_NOTE}{" "}
          <a
            href={WRITEUP_URL}
            target="_blank"
            rel="noreferrer"
            className="font-medium underline underline-offset-4"
          >
            Read the writeup
          </a>
          .
        </p>
        {withFeaturedLink ? (
          <p>
            <Link
              href={`/attempts/${FEATURED_ATTEMPT_ID}`}
              className="inline-flex items-center gap-1 font-medium underline underline-offset-4"
            >
              Jump to the one prompt injection that succeeded
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>{" "}
            <span className="text-muted-foreground">
              — gpt-oss-120b-groq, research-injection, repetition 1.
            </span>
          </p>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}
