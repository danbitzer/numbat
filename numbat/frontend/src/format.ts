// Time formatters shared by the charts, the tile and the flow wording.
// Pure (no window access) so they can be imported under `bun test`.

export function fmtDayTime(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtTime(ms: number): string {
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
