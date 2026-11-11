import { describe, expect, test } from "bun:test";
import { makeTicks } from "./ticks";

// Ticks sit on the clock in the machine's local zone (the browser's, live),
// so the checks are about spacing and alignment, not wall-clock strings.
const T0 = Date.parse("2026-11-11T10:07:00+10:30");
const H = 3_600_000;
const minutes = (ms: number) => new Date(ms).getMinutes();

function check(spanH: number, stepH: number, stepHours?: number) {
  const ticks = makeTicks(T0, T0 + spanH * H, stepHours);
  expect(ticks.length).toBeGreaterThan(0);
  expect(ticks[0]).toBeGreaterThanOrEqual(T0);
  expect(ticks[0]! - T0).toBeLessThan(stepH * H); // the first multiple at or after now
  for (let i = 1; i < ticks.length; i++) expect(ticks[i]! - ticks[i - 1]!).toBe(stepH * H);
  expect(ticks[ticks.length - 1]).toBeLessThanOrEqual(T0 + spanH * H);
  for (const t of ticks) expect(minutes(t) % (stepH < 1 ? stepH * 60 : 60)).toBe(0);
  return ticks;
}

describe("makeTicks adapts to the window", () => {
  test("an hour gets quarter-hours", () => expect(check(1, 0.25).length).toBe(4));
  test("six hours get hourly ticks", () => expect(check(6, 1).length).toBe(6));
  test("twelve hours get two-hourly ticks", () => expect(check(12, 2).length).toBe(6));
  test("a day gets three-hourly ticks", () => expect(check(24, 3).length).toBe(8));
  test("the full horizon keeps the two-hourly ticks it always had", () =>
    expect(check(36, 2).length).toBe(18));
  test("an explicit step is honoured", () => expect(check(6, 2, 2).length).toBe(3));
});
