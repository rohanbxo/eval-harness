/**
 * Display formatting. The rule that matters: a missing number is "unknown",
 * never a zero and never a guess (SPEC 6.3 - cost is null when pricing is
 * unknown).
 */

export const UNKNOWN = "unknown";

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return UNKNOWN;
  }
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatScore(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return UNKNOWN;
  }
  return value.toFixed(digits);
}

/** USD cost. `null` means LiteLLM had no pricing for the model - say so. */
export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return UNKNOWN;
  }
  if (value === 0) {
    return "$0.00";
  }
  if (value < 0.01) {
    return `$${value.toFixed(4)}`;
  }
  return `$${value.toFixed(2)}`;
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return UNKNOWN;
  }
  if (value < 1000) {
    return `${Math.round(value)} ms`;
  }
  return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} s`;
}

export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return UNKNOWN;
  }
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatInteger(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return UNKNOWN;
  }
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return UNKNOWN;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function shortHash(value: string | null | undefined, length = 8): string {
  if (!value) {
    return UNKNOWN;
  }
  return value.length <= length ? value : value.slice(0, length);
}

export function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** Signed delta, for the compare view. */
export function formatDelta(
  value: number | null | undefined,
  render: (n: number) => string,
): { text: string; direction: "up" | "down" | "flat" | "unknown" } {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return { text: UNKNOWN, direction: "unknown" };
  }
  if (Math.abs(value) < 1e-9) {
    return { text: render(0), direction: "flat" };
  }
  const sign = value > 0 ? "+" : "-";
  return { text: `${sign}${render(Math.abs(value))}`, direction: value > 0 ? "up" : "down" };
}

export function subtract(a: number | null | undefined, b: number | null | undefined): number | null {
  if (a === null || a === undefined || b === null || b === undefined) {
    return null;
  }
  return a - b;
}

export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}
