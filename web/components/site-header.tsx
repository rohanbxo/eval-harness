"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Activity } from "lucide-react";

import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { READ_ONLY } from "@/lib/read-only";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/", label: "Leaderboard", exact: true },
  { href: "/runs", label: "Runs", exact: false },
  { href: "/compare", label: "Compare", exact: false },
  { href: "/scenarios", label: "Scenarios", exact: false },
] as const;

export function SiteHeader() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/70">
      <div className="mx-auto flex h-14 w-full max-w-[1400px] items-center gap-6 px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2 font-semibold">
          <Activity className="h-4 w-4" />
          <span>EvalHarness</span>
        </Link>

        <nav className="flex flex-1 items-center gap-1 overflow-x-auto">
          {LINKS.map((link) => {
            const active = link.exact ? pathname === link.href : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm transition-colors",
                  active ? "bg-secondary font-medium text-secondary-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-2">
          {READ_ONLY ? null : (
            <Button size="sm" asChild>
              <Link href="/runs/new">New run</Link>
            </Button>
          )}
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
