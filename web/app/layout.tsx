import type { Metadata } from "next";

import { QueryProvider } from "@/components/query-provider";
import { SiteHeader } from "@/components/site-header";
import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/tooltip";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "EvalHarness",
    template: "%s · EvalHarness",
  },
  description: "Measure how well LLMs use tools in multi-turn agentic workflows.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-background font-sans text-foreground">
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
          <QueryProvider>
            <TooltipProvider delayDuration={150}>
              <SiteHeader />
              <main className="mx-auto w-full max-w-[1400px] px-4 py-8 sm:px-6">{children}</main>
            </TooltipProvider>
          </QueryProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
