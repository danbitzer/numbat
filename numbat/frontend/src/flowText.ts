// The flow vocabulary's wording: what the energy is doing, in the words a
// householder would use, plus the "why" (a price, a time) on the second
// line. The KEY comes from the backend (numbat/flow.py, action-first, so the
// tile can never disagree with the published instruction); the words live
// here. Tested against a non-technical persona: lead with the payoff
// ("Getting paid to…"), quote the price, never "idle", "forced", "PV".
import type { Explanation } from "./api";
import { fmtDayTime, fmtTime } from "./format";

export type FlowFamily =
  | "self"
  | "selling_solar"
  | "holding_back_solar"
  | "charging_from_grid"
  | "selling_stored_energy"
  | "running_on_grid";

// The strip colours by family: the four self-consumption flows share one
// colour (the battery chart underneath shows which), the overrides get their
// own so a negative-price day reads at a glance.
export const FLOW_FAMILY: Record<string, FlowFamily> = {
  storing_solar: "self",
  solar_running_house: "self",
  running_on_battery: "self",
  battery_empty: "self",
  waiting: "self",
  selling_solar: "selling_solar",
  holding_back_solar: "holding_back_solar",
  charging_from_grid: "charging_from_grid",
  selling_stored_energy: "selling_stored_energy",
  running_on_grid: "running_on_grid",
};

export const FLOW_FAMILY_LABEL: Record<FlowFamily, string> = {
  self: "solar & battery",
  selling_solar: "selling solar",
  holding_back_solar: "holding back solar",
  charging_from_grid: "charging from grid",
  selling_stored_energy: "selling stored energy",
  running_on_grid: "running on grid",
};

// Short per-flow words for the strip tooltip and the legend-less places.
export const FLOW_SHORT: Record<string, string> = {
  storing_solar: "storing solar",
  solar_running_house: "solar running the house",
  running_on_battery: "running on the battery",
  battery_empty: "battery empty",
  waiting: "nothing flowing",
  selling_solar: "selling solar",
  holding_back_solar: "holding back solar",
  charging_from_grid: "charging from the grid",
  selling_stored_energy: "selling stored energy",
  running_on_grid: "running on the grid",
};

// Older payloads (or a plan never annotated) carry no flow: fall back to the
// action so the strip still colours sensibly.
export function flowOf(flow: string | undefined, action: string): string {
  if (flow) return flow;
  return (
    {
      charge: "charging_from_grid",
      discharge: "selling_stored_energy",
      hold: "running_on_grid",
      curtail: "holding_back_solar",
      no_charge: "selling_solar",
    }[action] ?? "waiting"
  );
}

export const familyOf = (flow: string): FlowFamily => FLOW_FAMILY[flow] ?? "self";

// Prices in whole cents below a dollar ("8c", "−3c"; a rounded zero has no
// sign), dollars from a dollar up ("$1.50").
export function cents(x: number): string {
  const c = Math.round(x * 100);
  if (c === 0) return "0c";
  const sign = c < 0 ? "−" : "";
  const a = Math.abs(c);
  return a >= 100 ? `${sign}$${(a / 100).toFixed(2)}` : `${sign}${a}c`;
}
const kw = (x: number) => `${x.toFixed(1)} kW`;
// a time more than 12 h away needs its day ("Tue 10:00 am"), else just the time
const at = (iso: string, from?: number) => {
  const ms = Date.parse(iso);
  return from != null && Math.abs(ms - from) > 12 * 3_600_000 ? fmtDayTime(ms) : fmtTime(ms);
};

export interface FlowWords {
  label: string;
  sub: string;
}

type FlowInfo = NonNullable<Explanation["flow"]>;

/** Tile label + sub-label for step 0. `v` is the explanation's step-0
 * numbers; `f` the flow block (key, modifiers, look-ahead facts). */
export function flowWords(
  f: FlowInfo,
  v: Explanation["values"],
  opts: { liveSpike?: boolean; action?: string; now?: number } = {},
): FlowWords {
  const capped = !!f.export_capped;
  const pvOff = !!f.pv_off;
  const soc = v.soc_start_pct != null ? `${Math.round(v.soc_start_pct)}%` : null;
  const socEnd = v.soc_end_pct != null ? `${Math.round(v.soc_end_pct)}%` : null;
  // "full" means at the configured ceiling (soc_max, not 100%); without it
  // in the payload, a battery that isn't charging and has no fill ahead is
  // full for all practical purposes. A battery with room that ISN'T
  // charging while solar is held back is the plan deferring the fill.
  const charging = v.battery_kw > 0.05;
  const full =
    v.soc_start_pct != null && v.soc_max_pct != null
      ? v.soc_start_pct >= v.soc_max_pct - 1
      : !charging && !f.next_fill_time;
  const when = (iso: string) => at(iso, opts.now);
  // the stored energy's next use, when the plan knows it: the house at a
  // dearer buy price ("12c now, 38c at 6 am") or a forced export at a
  // feed-in price ("to sell at 60c at 5 pm"); a use that isn't dearer than
  // now doesn't explain a hold (a daily target or a spike reserve does) and
  // is left unsaid
  const usePrice = f.next_use_price ?? f.next_use_buy;
  const useKind = f.next_use_kind ?? "house";
  const useSays =
    f.next_use_time && usePrice != null
      ? useKind === "export"
        ? `to sell at ${cents(usePrice)} at ${when(f.next_use_time)}`
        : usePrice > v.buy
          ? `${cents(v.buy)} now, ${cents(usePrice)} at ${when(f.next_use_time)}`
          : null
      : null;
  const later = useSays ?? `${cents(v.buy)} now`;
  const paused = pvOff ? "; panels paused" : "";
  let words: FlowWords;
  switch (f.key) {
    case "storing_solar":
      words = capped
        ? {
            label: "Storing solar, not exporting",
            sub: `selling price is ${cents(v.sell)} — nothing goes out; surplus fills the battery`,
          }
        : {
            label: "Storing solar",
            sub: `surplus solar is filling the battery${soc && socEnd ? ` (${soc} → ${socEnd})` : ""}`,
          };
      break;
    case "solar_running_house":
      words = { label: "Solar running the house", sub: "solar covers the house; battery untouched" };
      break;
    case "running_on_battery":
      words = {
        label: "Running on the battery",
        sub: `grid is ${cents(v.buy)} — using stored energy${soc ? ` (${soc})` : ""}`,
      };
      break;
    case "selling_solar":
      words =
        opts.action === "no_charge" && f.next_fill_time
          ? {
              label: "Selling solar now, filling later",
              sub: `selling at ${cents(v.sell)} now; the battery fills from ${
                f.next_fill_source === "grid" ? "the grid" : "solar"
              } at ${when(f.next_fill_time)}`,
            }
          : {
              label: "Selling solar",
              sub: `${full ? "battery full; " : ""}exporting the surplus at ${cents(v.sell)}`,
            };
      break;
    case "holding_back_solar": {
      const spill = f.pv_spill_kw != null && f.pv_spill_kw > 0 ? `${kw(f.pv_spill_kw)} of solar` : "solar";
      // why the battery isn't taking the spill: full, charging as fast as
      // it can, or the plan is deferring the fill (a later free window)
      const battery = full
        ? ""
        : charging
          ? "; the battery is charging at its maximum"
          : f.next_fill_time
            ? `; the battery fills at ${when(f.next_fill_time)}`
            : "; the battery isn't charging yet";
      words = {
        label: full ? "Battery full, holding back solar" : "Holding back solar",
        sub:
          v.sell < 0
            ? `selling price is ${cents(v.sell)} — exporting would cost you${battery}`
            : `export limit reached — ${spill} has nowhere to go${battery}`,
      };
      break;
    }
    case "charging_from_grid": {
      // grid_import_kw is the meter (house + charge); the charge itself is
      // battery_kw, of which any part the import doesn't cover came from solar
      const solarIn = Math.max(0, v.battery_kw - v.grid_import_kw);
      const charge = `charging at ${kw(v.battery_kw)}${solarIn > 0.05 ? ` (${kw(solarIn)} of it solar)` : ""}`;
      words =
        v.buy < 0
          ? {
              label: "Getting paid to fill the battery",
              sub: `they're paying you ${cents(-v.buy)} to take power — ${charge}${paused}`,
            }
          : { label: "Charging from the grid", sub: `${charge} · ${later}` };
      break;
    }
    case "selling_stored_energy":
      words = {
        label: "Selling stored energy",
        sub: `${opts.liveSpike ? "price spike — " : ""}${kw(v.grid_export_kw)} at ${cents(v.sell)}${
          f.soc_min_ahead_pct != null ? ` · keeps at least ${Math.round(f.soc_min_ahead_pct)}% over the next 12 h` : ""
        }`,
      };
      break;
    case "running_on_grid":
      words =
        v.buy < 0
          ? {
              label: "Getting paid to use the grid",
              sub: `they're paying you ${cents(-v.buy)} — house on the grid, battery held${paused}`,
            }
          : {
              label: f.next_use_time && useSays ? `Saving the battery for ${when(f.next_use_time)}` : "Saving the battery",
              sub: `${later} — house on the grid${soc ? ` (${soc})` : ""}`,
            };
      break;
    case "battery_empty":
      words = f.next_fill_time
        ? {
            label: f.next_fill_source === "grid" ? "Waiting for a cheap price" : "Waiting for sun",
            sub: `battery at ${soc ?? "its floor"} — fills from ${
              f.next_fill_source === "grid" ? "the grid" : "solar"
            } at ${when(f.next_fill_time)}`,
          }
        : { label: "Waiting", sub: `battery at ${soc ?? "its floor"}; waiting for sun or a cheap price` };
      break;
    default:
      words = { label: "Nothing flowing", sub: soc ? `battery at ${soc}` : "" };
  }
  // modifiers the words above didn't already express
  if (pvOff && !words.sub.includes("panels paused")) {
    words.sub += "; panels paused (buy price is negative)";
  }
  if (capped && !["storing_solar", "holding_back_solar"].includes(f.key)) {
    words.sub += " · export capped";
  }
  return words;
}
