export type TerminalTimeRange = "1d" | "3d" | "5d" | "7d" | "14d" | "30d" | "all";

export const DEFAULT_TERMINAL_TIME_RANGE: TerminalTimeRange = "7d";

export const TERMINAL_TIME_RANGE_OPTIONS: Array<{ value: TerminalTimeRange; label: string }> = [
  { value: "1d", label: "1天" },
  { value: "3d", label: "3天" },
  { value: "5d", label: "5天" },
  { value: "7d", label: "7天" },
  { value: "14d", label: "2周" },
  { value: "30d", label: "1个月" },
  { value: "all", label: "全部" }
];

export function isTerminalTimeRange(value: unknown): value is TerminalTimeRange {
  return TERMINAL_TIME_RANGE_OPTIONS.some((option) => option.value === value);
}
