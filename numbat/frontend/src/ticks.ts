// Axis ticks for the plan charts. Pure (no window access) so it can be
// imported under `bun test`.

/** Ticks aligned to the clock in the plan's local timezone, spaced to suit
 * the window: quarter-hours for an hour, hourly up to 8 h, two-hourly up to
 * 14 h, three-hourly to a day, and the long-standing two-hourly beyond. */
export function makeTicks(t0: number, tEnd: number, stepHours?: number): number[] {
  const spanH = (tEnd - t0) / 3_600_000;
  const step = stepHours ?? (spanH <= 2 ? 0.25 : spanH <= 8 ? 1 : spanH <= 14 ? 2 : spanH <= 26 ? 3 : 2);
  const stepMs = step * 3_600_000;
  const first = new Date(t0);
  first.setMinutes(0, 0, 0);
  // the first tick is the first multiple of the step (from the top of that
  // hour) at or after t0
  let t = first.getTime();
  while (t < t0) t += stepMs;
  const ticks: number[] = [];
  for (; t <= tEnd; t += stepMs) ticks.push(t);
  return ticks;
}
