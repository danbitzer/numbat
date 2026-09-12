// The user-visible surface of the flow vocabulary: the words. Run with
// `bun test` (no runner to install — Bun's built-in). Scenarios S1–S11 from
// the vocabulary review plus the wording traps the code review found.
import { describe, expect, test } from "bun:test";
import { cents, flowOf, flowWords, familyOf } from "./flowText";

const T0 = Date.parse("2026-09-13T01:00:00Z"); // 10:30 Adelaide
const later = (h: number) => new Date(T0 + h * 3_600_000).toISOString();

function values(over: Partial<Parameters<typeof flowWords>[1]> = {}) {
  return {
    buy: 0.3,
    sell: 0.08,
    pv_kw: 0,
    load_kw: 1,
    soc_start_kwh: 26,
    soc_end_kwh: 26,
    soc_start_pct: 58,
    soc_end_pct: 58,
    battery_kw: 0,
    grid_import_kw: 0,
    grid_export_kw: 0,
    interval_cost: 0,
    ...over,
  };
}

describe("cents", () => {
  test("whole cents below a dollar, dollars from a dollar, no signed zero", () => {
    expect(cents(0.08)).toBe("8c");
    expect(cents(-0.03)).toBe("−3c");
    expect(cents(1.5)).toBe("$1.50");
    expect(cents(0.995)).toBe("$1.00");
    expect(cents(-0.004)).toBe("0c");
  });
});

describe("flowOf / familyOf", () => {
  test("falls back to the action when the backend sent no flow", () => {
    expect(flowOf(undefined, "charge")).toBe("charging_from_grid");
    expect(flowOf("", "hold")).toBe("running_on_grid");
    expect(flowOf("storing_solar", "idle")).toBe("storing_solar");
    expect(familyOf("running_on_battery")).toBe("self");
    expect(familyOf("nonsense")).toBe("self");
  });
});

describe("flowWords", () => {
  test("S1 storing solar quotes the SoC change", () => {
    const w = flowWords({ key: "storing_solar" }, values({ battery_kw: 4, pv_kw: 5, soc_end_pct: 66 }));
    expect(w.label).toBe("Storing solar");
    expect(w.sub).toContain("58% → 66%");
  });
  test("S2 storing solar with the cap on folds the modifier into the label", () => {
    const w = flowWords({ key: "storing_solar", export_capped: true }, values({ battery_kw: 4, sell: -0.03 }));
    expect(w.label).toBe("Storing solar, not exporting");
    expect(w.sub).toContain("−3c");
    expect(w.sub).not.toContain("export capped");
  });
  test("S3 holding back solar at negative feed-in vs an export-limit cut", () => {
    const neg = flowWords(
      { key: "holding_back_solar", export_capped: true, pv_spill_kw: 6.2 },
      values({ sell: -0.03, soc_start_pct: 95, soc_max_pct: 95 }),
    );
    expect(neg.label).toBe("Battery full, holding back solar");
    expect(neg.sub).toContain("exporting would cost you");
    expect(neg.sub).not.toContain("battery fills");
    // 75% and not charging: the plan is deferring the fill, not full
    const deferred = flowWords(
      { key: "holding_back_solar", export_capped: true, pv_spill_kw: 4, next_fill_time: later(3), next_fill_source: "solar" },
      values({ sell: -0.03, soc_start_pct: 75, soc_max_pct: 95 }),
      { now: T0 },
    );
    expect(deferred.label).toBe("Holding back solar");
    expect(deferred.sub).toContain("the battery fills from solar at");
    const lim = flowWords({ key: "holding_back_solar", pv_spill_kw: 3 }, values({ sell: 0.08, battery_kw: 4, soc_start_pct: 75, soc_max_pct: 95 }));
    expect(lim.label).toBe("Holding back solar");
    expect(lim.sub).toContain("export limit reached");
    expect(lim.sub).toContain("3.0 kW of solar");
    expect(lim.sub).toContain("charging at its maximum");
  });
  test("S4 no_charge sells now and names the free fill", () => {
    const w = flowWords(
      { key: "selling_solar", next_fill_time: later(2), next_fill_source: "solar" },
      values({ sell: 0.05, grid_export_kw: 3 }),
      { action: "no_charge", now: T0 },
    );
    expect(w.label).toBe("Selling solar now, filling later");
    expect(w.sub).toContain("fills from solar at");
  });
  test("S5 running on the battery quotes the buy price", () => {
    const w = flowWords({ key: "running_on_battery" }, values({ buy: 0.4, battery_kw: -2 }));
    expect(w.label).toBe("Running on the battery");
    expect(w.sub).toContain("grid is 40c");
  });
  test("S6 charging from the grid uses the comparison format and the charge, not the meter", () => {
    const w = flowWords(
      { key: "charging_from_grid", next_use_time: later(5), next_use_kind: "house", next_use_price: 0.3 },
      values({ buy: 0.08, battery_kw: 5, grid_import_kw: 5.5 }),
      { now: T0 },
    );
    expect(w.label).toBe("Charging from the grid");
    expect(w.sub).toContain("charging at 5.0 kW");
    expect(w.sub).toContain("8c now, 30c at");
    expect(w.sub).not.toContain("5.5 kW");
  });
  test("S7 selling stored energy names the spike and the floor", () => {
    const w = flowWords(
      { key: "selling_stored_energy", soc_min_ahead_pct: 30 },
      values({ sell: 1.5, battery_kw: -12, grid_export_kw: 11.5 }),
      { liveSpike: true },
    );
    expect(w.sub).toContain("price spike — 11.5 kW at $1.50");
    expect(w.sub).toContain("keeps at least 30%");
  });
  test("S8/S9 negative buy leads with the payoff, panels paused in the sub-label", () => {
    const hold = flowWords({ key: "running_on_grid", pv_off: true, export_capped: true }, values({ buy: -0.13 }));
    expect(hold.label).toBe("Getting paid to use the grid");
    expect(hold.sub).toContain("they're paying you 13c");
    expect(hold.sub).toContain("panels paused");
    expect(hold.sub.match(/panels paused/g)?.length).toBe(1);
    const fill = flowWords({ key: "charging_from_grid", pv_off: true }, values({ buy: -0.13, battery_kw: 10, grid_import_kw: 11 }));
    expect(fill.label).toBe("Getting paid to fill the battery");
  });
  test("S10 saving the battery names the time only when the later price is dearer", () => {
    const w = flowWords(
      { key: "running_on_grid", next_use_time: later(7), next_use_kind: "house", next_use_price: 0.38 },
      values({ buy: 0.12 }),
      { now: T0 },
    );
    expect(w.label).toMatch(/^Saving the battery for /);
    expect(w.sub).toContain("12c now, 38c at");
    const cheaperLater = flowWords(
      { key: "running_on_grid", next_use_time: later(7), next_use_kind: "house", next_use_price: 0.1 },
      values({ buy: 0.3 }),
      { now: T0 },
    );
    expect(cheaperLater.label).toBe("Saving the battery");
    expect(cheaperLater.sub).not.toContain("10c at");
    const sale = flowWords(
      { key: "running_on_grid", next_use_time: later(7), next_use_kind: "export", next_use_price: 0.6 },
      values({ buy: 0.12 }),
      { now: T0 },
    );
    expect(sale.sub).toContain("to sell at 60c at");
  });
  test("selling at a loss to make room for a paid refill says so", () => {
    const w = flowWords(
      { key: "selling_stored_energy", next_fill_time: later(4), next_fill_source: "grid", next_fill_price: -0.15 },
      values({ sell: -0.008, battery_kw: -10, grid_export_kw: 8.6 }),
      { now: T0 },
    );
    expect(w.label).toBe("Making room to be paid to refill");
    expect(w.sub).toContain("paid 15c/kWh");
    const deferred = flowWords(
      { key: "holding_back_solar", export_capped: true, pv_spill_kw: 8.9, next_fill_time: later(2.5), next_fill_source: "grid", next_fill_price: -0.15 },
      values({ sell: -0.083, battery_kw: -0.2, soc_start_pct: 41, soc_max_pct: 100 }),
      { now: T0 },
    );
    expect(deferred.sub).toContain("fills from the grid at");
    expect(deferred.sub).toContain("paid 15c/kWh");
  });
  test("S11 an empty battery waits for what fills it next", () => {
    const sun = flowWords({ key: "battery_empty", next_fill_time: later(4), next_fill_source: "solar" }, values({ soc_start_pct: 5 }), { now: T0 });
    expect(sun.label).toBe("Waiting for sun");
    const grid = flowWords({ key: "battery_empty", next_fill_time: later(1), next_fill_source: "grid" }, values({ soc_start_pct: 5 }), { now: T0 });
    expect(grid.label).toBe("Waiting for a cheap price");
    expect(grid.sub).toContain("battery at 5%");
  });
  test("a fill more than 12 h away carries its day", () => {
    const w = flowWords({ key: "battery_empty", next_fill_time: later(20), next_fill_source: "solar" }, values(), { now: T0 });
    expect(w.sub).toMatch(/at \w{3}/); // weekday prefix from fmtDayTime
  });
  test("export capped is appended once where the words don't already say it", () => {
    const w = flowWords({ key: "running_on_battery", export_capped: true }, values({ battery_kw: -1 }));
    expect(w.sub.endsWith("· export capped")).toBe(true);
  });
});
