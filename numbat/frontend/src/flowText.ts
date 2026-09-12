// The flow vocabulary's wording: what the energy is doing, in the words a
// householder would use, plus the "why" (a price, a time) on the second
// line. The KEY comes from the backend (numbat/flow.py, action-first, so the
// tile can never disagree with the published instruction); the words live
// here. Tested against a non-technical persona: lead with the payoff
// ("Getting paid to…"), quote the price, never "idle", "forced", "PV".
import type { Explanation } from "./api";
import { fmtTime } from "./theme";

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

// Prices in cents below a dollar ("8c", "−3c"), dollars above ("$1.50").
export function cents(x: number): string {
  const sign = x < 0 ? "−" : "";
  const a = Math.abs(x);
  return a >= 1 ? `${sign}$${a.toFixed(2)}` : `${sign}${Math.round(a * 100)}c`;
}
const kw = (x: number) => `${x.toFixed(1)} kW`;
const at = (iso: string) => fmtTime(Date.parse(iso));

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
  opts: { liveSpike?: boolean; action?: string } = {},
): FlowWords {
  const capped = !!f.export_capped;
  const pvOff = !!f.pv_off;
  const soc = v.soc_start_pct != null ? `${Math.round(v.soc_start_pct)}%` : null;
  const socEnd = v.soc_end_pct != null ? `${Math.round(v.soc_end_pct)}%` : null;
  const full = v.soc_start_pct != null && v.soc_start_pct >= 98;
  const later =
    f.next_use_time && f.next_use_buy != null
      ? `${cents(v.buy)} now, ${cents(f.next_use_buy)} at ${at(f.next_use_time)}`
      : `${cents(v.buy)} now`;
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
              } at ${at(f.next_fill_time)}`,
            }
          : {
              label: "Selling solar",
              sub: `${full ? "battery full; " : ""}exporting the surplus at ${cents(v.sell)}`,
            };
      break;
    case "holding_back_solar":
      words = {
        label: full ? "Battery full, holding back solar" : "Holding back solar",
        sub: `selling price is ${cents(v.sell)} — exporting would cost you${
          full ? "" : "; the battery is charging at its maximum"
        }`,
      };
      break;
    case "charging_from_grid": {
      const solarIn = Math.max(0, v.battery_kw - v.grid_import_kw);
      words =
        v.buy < 0
          ? {
              label: "Getting paid to fill the battery",
              sub: `they're paying you ${cents(-v.buy)} to take power — charging at ${kw(v.grid_import_kw)}${paused}`,
            }
          : {
              label: "Charging from the grid",
              sub: `${kw(v.grid_import_kw)} from the grid${solarIn > 0.05 ? ` + ${kw(solarIn)} solar` : ""} · ${later}`,
            };
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
              label: f.next_use_time ? `Saving the battery for ${at(f.next_use_time)}` : "Saving the battery",
              sub: `${later} — house on the grid${soc ? ` (${soc})` : ""}`,
            };
      break;
    case "battery_empty":
      words = f.next_fill_time
        ? {
            label: f.next_fill_source === "grid" ? "Waiting for a cheap price" : "Waiting for sun",
            sub: `battery at ${soc ?? "its floor"} — fills from ${
              f.next_fill_source === "grid" ? "the grid" : "solar"
            } at ${at(f.next_fill_time)}`,
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
