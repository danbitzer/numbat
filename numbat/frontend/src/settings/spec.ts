// The Settings form, described as data: sections -> fields, with inline help
// ported from DOCS.md (which stays as the deep-dive; this is the canonical
// in-app field documentation). The server's pydantic Settings model is the
// validation authority — min/max here are input hints only.

export type FieldKind = "entity" | "number" | "boolean" | "select" | "text" | "time";

export interface FieldSpec {
  path: string; // dot path in the config document, e.g. "battery.capacity_kwh"
  label: string;
  kind: FieldKind;
  help: string;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
  /** No server-side default — must be filled before the form can save. */
  required?: boolean;
  /** Stored as a fraction/multiplier, displayed ×100 with a "%" unit.
   * min/max/step/default in the spec are in DISPLAY (percent) units. */
  percent?: boolean;
  /** Default shown for fresh installs (mirrors the pydantic default). */
  default?: string | boolean;
  options?: { value: string; label: string }[];
  /** Entity picker filter: include only these domains. */
  domains?: string[];
  /** Entity picker: empty means "not used" instead of invalid. */
  optional?: boolean;
  /** Only shown in Test mode (the field feeds simulations, not live
   * planning). Still part of ALL_FIELDS so the live form round-trips the
   * stored value instead of wiping it on save. */
  testOnly?: boolean;
}

export interface SectionSpec {
  id: string;
  title: string;
  description: string;
  fields: FieldSpec[];
}

const entity = (
  path: string,
  label: string,
  help: string,
  domains: string[],
  opts: Partial<FieldSpec> = {},
): FieldSpec => ({ path, label, help, domains, kind: "entity", required: !opts.optional, ...opts });

const number = (
  path: string,
  label: string,
  help: string,
  opts: Partial<FieldSpec> = {},
): FieldSpec => ({ path, label, help, kind: "number", ...opts });

export const SECTIONS: SectionSpec[] = [
  {
    id: "entities",
    title: "Entities",
    description:
      "Point Numbat at your Home Assistant entities. Amber Express's forecast " +
      "attributes live on the price sensors themselves, so the two price " +
      "entities cover both live prices and forecasts.",
    fields: [
      entity("entities.buy_price", "Buy price", "Amber Express general price sensor ($/kWh).", [
        "sensor",
      ]),
      entity(
        "entities.sell_price",
        "Feed-in price",
        "Amber Express feed-in price sensor ($/kWh).",
        ["sensor"],
      ),
      entity(
        "entities.price_spike",
        "Price spike",
        "Amber Express price-spike binary sensor — enables the spike reserve strategy.",
        ["binary_sensor"],
        { optional: true, default: "" },
      ),
      entity(
        "entities.pv_forecast_today",
        "PV forecast (today)",
        "Open-Meteo Solar Forecast energy production today.",
        ["sensor"],
      ),
      entity(
        "entities.pv_forecast_tomorrow",
        "PV forecast (tomorrow)",
        "Open-Meteo Solar Forecast energy production tomorrow.",
        ["sensor"],
      ),
      entity("entities.battery_soc", "Battery SoC", "Battery level sensor (%).", ["sensor"]),
      entity(
        "entities.battery_power",
        "Battery power",
        "Battery power sensor (W or kW). Set the sign convention under Battery.",
        ["sensor"],
      ),
      entity(
        "entities.weather",
        "Weather",
        "Any weather entity with an hourly forecast — feeds the temperature response. " +
          "Without it, load planning is time-of-day only.",
        ["weather"],
        { optional: true, default: "" },
      ),
      entity(
        "entities.load_power",
        "House load power",
        "A sensor measuring total household consumption (W or kW). The load " +
          "forecast is learned from its history. Strongly recommended: without it Numbat " +
          "plans with ZERO house load.",
        ["sensor"],
        { optional: true, default: "" },
      ),
      entity(
        "entities.outdoor_temp",
        "Outdoor temperature",
        "Outdoor temperature sensor with long-term statistics — enables the learned " +
          "temperature response (extra kW per degree of cooling/heating).",
        ["sensor"],
        { optional: true, default: "" },
      ),
      entity(
        "entities.pv_power",
        "PV power (actual)",
        "Your inverter's actual PV generation power sensor (W or kW) — distinct " +
          "from the forecast sensors above. Time travel replays real solar from " +
          "its history; without it, historical replays assume zero PV.",
        ["sensor"],
        { optional: true, default: "", testOnly: true },
      ),
    ],
  },
  {
    id: "battery",
    title: "Battery",
    description:
      "Physical parameters of the battery and how its wear and reserves are priced.",
    fields: [
      number("battery.capacity_kwh", "Capacity", "Usable battery capacity.", {
        unit: "kWh",
        min: 0.5,
        step: 0.1,
        required: true,
      }),
      number("battery.max_charge_kw", "Max charge power", "Battery-side charging limit.", {
        unit: "kW",
        min: 0.1,
        step: 0.1,
        required: true,
      }),
      number(
        "battery.max_discharge_kw",
        "Max discharge power",
        "Battery-side everyday discharge limit (wear-conscious — the spike section can " +
          "temporarily raise it).",
        { unit: "kW", min: 0.1, step: 0.1, required: true },
      ),
      number("battery.efficiency_charge", "Charge efficiency", "AC→DC charge efficiency.", {
        unit: "%",
        percent: true,
        min: 50,
        max: 100,
        step: 1,
        default: "95",
      }),
      number(
        "battery.efficiency_discharge",
        "Discharge efficiency",
        "DC→AC discharge efficiency.",
        { unit: "%", percent: true, min: 50, max: 100, step: 1, default: "95" },
      ),
      number(
        "battery.soc_min",
        "Planning reserve (SoC min)",
        "Numbat's planning reserve, NOT the inverter's minimum SoC — set it above the " +
          "inverter's own floor as insurance against forecast error. Deliberate " +
          "discharges stop here; idle self-consumption can still drain below it, which " +
          "is what the reserve insures against.",
        { unit: "%", percent: true, min: 0, max: 100, step: 1, default: "10" },
      ),
      number("battery.soc_max", "SoC max", "Upper SoC bound as a percentage of capacity.", {
        unit: "%",
        percent: true,
        min: 0,
        max: 100,
        step: 1,
        default: "100",
      }),
      number(
        "battery.wear_cost_per_kwh",
        "Wear cost",
        "Degradation cost charged against EVERY discharged kWh — including serving " +
          "your own house: every cent here raises the import price the plan will pay " +
          "before touching the battery. Base it on the wearing part's replacement " +
          "cost ÷ cycle throughput — closer to a future cell replacement than the " +
          "full installed price (inverter and labour wear far slower, and calendar " +
          "aging usually reaches the cells first). Realistic lithium ~0.5–3c. Use " +
          "the min battery export spread for " +
          "\"only sell when it's worth it\" selectivity instead.",
        { unit: "$/kWh", min: 0, step: 0.01, default: "0.03" },
      ),
      {
        path: "battery.allow_grid_charge",
        label: "Allow grid charging",
        kind: "boolean",
        default: true,
        help: "Permit charging the battery from the grid (not just PV surplus).",
      },
      {
        path: "battery.power_convention",
        label: "Power sign convention",
        kind: "select",
        default: "charge_negative",
        options: [
          { value: "charge_negative", label: "positive = discharging" },
          { value: "charge_positive", label: "positive = charging" },
        ],
        help: "Which sign your battery power sensor reports while charging.",
      },
      number(
        "battery.daily_target_soc",
        "Daily full-charge target",
        "Daily insurance target SoC (% of capacity; 0 disables). A rational " +
          "optimizer only charges enough for the forecast — this softly requires the " +
          "battery at the target from the time below, HELD through the evening peak, so " +
          "unforecast spikes and surprise usage find it charged. Freed to discharge once " +
          "the hold window ends.",
        { unit: "%", percent: true, min: 0, max: 100, step: 5, default: "0" },
      ),
      {
        path: "battery.daily_target_time",
        label: "Daily target time",
        kind: "time",
        default: "15:00",
        help: "Local time the target window starts (default 15:00, before the evening ramp).",
      },
      number(
        "battery.daily_target_hold_hours",
        "Daily target hold",
        "How long to hold the target as a floor after the target time — the window " +
          "through the evening peak (e.g. 4h = full from 3pm to 7pm). 0 pins a single " +
          "instant instead of a floor.",
        { unit: "h", min: 0, max: 24, step: 0.5, default: "4" },
      ),
      number(
        "battery.daily_target_penalty_per_kwh",
        "Daily target penalty",
        "Willingness-to-pay per kWh-HOUR of shortfall below the target floor — anything " +
          "cheaper WILL be bought to fill it. It accrues over the hold window: the " +
          "default $0.05 over a 4 h hold makes a full-window missing kWh worth up to " +
          "$0.20. Raise it, or use the price multiple below, if the battery still " +
          "won't reach the target.",
        { unit: "$/kWh·h", min: 0, max: 10, step: 0.01, default: "0.05" },
      ),
      number(
        "battery.daily_target_penalty_price_multiple",
        "Daily target price multiple",
        "Works WITH the fixed penalty above, not instead of it: each solve uses " +
          "whichever is higher — the fixed penalty, or this percentage of the median " +
          "forward import price. Lets the penalty track the tariff so the target " +
          "still gets filled on dear days. 0 disables the scaling (fixed penalty " +
          "only). It accrues per hour of the hold window, so modest values dominate: " +
          "100% over a 4 h hold makes a full-window missing kWh worth ~4× the going " +
          "rate — 50–150% is plenty, and larger values only raise what an " +
          "extreme-priced day may cost.",
        { unit: "%", percent: true, min: 0, max: 1000, step: 5, default: "0" },
      ),
    ],
  },
  {
    id: "grid",
    title: "Grid connection",
    description:
      "Limits of the grid connection at the meter — distinct from the battery's own " +
      "power limits; the optimizer respects both simultaneously.",
    fields: [
      number(
        "grid.import_limit_kw",
        "Import limit",
        "Maximum net draw from the grid for the whole house (connection/main-breaker capacity).",
        { unit: "kW", min: 0.1, step: 0.5, required: true },
      ),
      number(
        "grid.export_limit_kw",
        "Export limit",
        "Maximum net feed-in allowed by your DNSP/connection agreement. Caps what " +
          "reaches the grid regardless of battery discharge (export = battery + PV − load). " +
          "If you raise the spike discharge cap, raise this to match or the extra power " +
          "has nowhere to go.",
        { unit: "kW", min: 0, step: 0.5, required: true },
      ),
      number(
        "grid.min_battery_export_price",
        "Min battery export price",
        "Lowest feed-in price at which Numbat will discharge the battery to the grid. " +
          "Below it the battery still covers the house but won't sell stored energy; " +
          "PV surplus can still export. Blank = no manual floor (the automatic export " +
          "deadband under Optimizer may still apply).",
        { unit: "$/kWh", step: 0.01 },
      ),
    ],
  },
  {
    id: "load",
    title: "Load forecast",
    description:
      "The house load forecast is learned from your consumption history (see the " +
      "load sensor under Entities) — nothing to configure beyond an optional safety margin.",
    fields: [
      number(
        "load.buffer",
        "Forecast buffer",
        "Safety margin on the learned forecast: 10% plans for 10% more house load " +
          "everywhere, including temperature-driven peaks. The learned profile is a " +
          "mean — buffer it if you'd rather the planner run conservative. Distinct " +
          "from the SoC reserve and the daily target, which shape battery policy " +
          "rather than the forecast.",
        { unit: "%", percent: true, min: 0, max: 100, step: 5, default: "0" },
      ),
    ],
  },
  {
    id: "optimizer",
    title: "Optimizer",
    description: "Horizon, solver limits and behavioral thresholds.",
    fields: [
      number(
        "optimizer.horizon_hours",
        "Horizon",
        "How far ahead each plan looks. Longer sees more of tomorrow's solar; beyond " +
          "the price forecast the tail is padded (shaded on the dashboard).",
        { unit: "h", min: 2, max: 72, step: 1, default: "36" },
      ),
      {
        path: "optimizer.terminal_soc_value",
        label: "Hold value (terminal SoC value)",
        kind: "text",
        default: "auto",
        help:
          "What a stored kWh is worth at the horizon end, in $/kWh (NOT a target SoC). " +
          "\"auto\" anchors it to rebuy cost — the cheapest forward import grossed up for " +
          "charge losses — scaled and floored below. Without it the optimizer would dump " +
          "the battery before the horizon. Enter \"auto\" or a fixed number.",
      },
      number(
        "optimizer.hold_value_floor",
        "Hold value floor",
        "Lower bound on the auto hold value ($/kWh), so a cheap day never values stored " +
          "energy at ~$0 (which was what made the battery sell cheap). Predbat uses ~1c.",
        { unit: "$/kWh", min: 0, step: 0.01, default: "0.01" },
      ),
      number(
        "optimizer.hold_value_scaling",
        "Hold value scaling",
        "Scales the auto hold value. Above 100% makes the battery holdier — but " +
          "also makes it IMPORT to stockpile (energy valued above its rebuy cost " +
          "turns cheap-window charging into free money in the model). Below 100% " +
          "trades more freely. Leave at 100% unless you understand that trade; " +
          "prefer the export spread or daily target for keeping more in the tank.",
        { unit: "%", percent: true, min: 0, max: 500, step: 5, default: "100" },
      ),
      number(
        "optimizer.min_battery_export_spread",
        "Min battery export spread",
        "Automatic export deadband: the battery only sells to the grid when the feed-in " +
          "beats the value of holding by at least this margin, killing pennies-margin " +
          "churn. 0 = off (sell whenever marginally profitable). The automatic " +
          "counterpart to grid min battery export price.",
        { unit: "$/kWh", min: 0, max: 10, step: 0.01, default: "0.05" },
      ),
      number(
        "optimizer.import_penalty_per_kwh",
        "Import reluctance",
        "A virtual toll added to every imported kWh in the planning maths (never in " +
          "the displayed costs) — biases the plan toward solar and stored energy over " +
          "importing, so import-now-to-sell-later bets need bigger margins. A " +
          "risk-preference knob for people who'd rather miss a forecast sell than " +
          "import now. Skipped when the buy price is negative (getting paid to " +
          "charge stays attractive). May need a higher daily-target penalty to " +
          "still fill via the grid. 0 = off.",
        { unit: "$/kWh", min: 0, max: 10, step: 0.01, default: "0.05" },
      ),
      number(
        "optimizer.action_switch_threshold_dollars",
        "Action switch threshold",
        "Hysteresis: the current action only changes if switching improves the horizon " +
          "objective by more than this.",
        { unit: "$", min: 0, max: 10, step: 0.01, default: "0.02" },
      ),
      number(
        "optimizer.forecast_haircut",
        "Sell price forecast haircut",
        "Shaves this share of the above-median excess off sell prices more than 6h " +
          "out, so distant phantom spikes don't distort near-term decisions (the spike " +
          "reserve reads raw prices, unaffected). Off by default: Amber's advanced " +
          "predicted pricing already tempers over-forecast spikes — turn it up if your " +
          "price sensor uses raw AEMO-style forecasts.",
        { unit: "%", percent: true, min: 0, max: 100, step: 5, default: "0" },
      ),
    ],
  },
  {
    id: "spike",
    title: "Spike strategy",
    description:
      "When Amber flags a potential price spike within the lookahead, Numbat softly " +
      "reserves energy in the battery so it can sell into the spike if it confirms.",
    fields: [
      number("spike.lookahead_hours", "Lookahead", "How far ahead to honor potential spikes.", {
        unit: "h",
        min: 0,
        max: 48,
        step: 0.5,
        default: "4",
      }),
      number("spike.reserve_kwh", "Reserve", "Energy kept in the battery while a spike looms.", {
        unit: "kWh",
        min: 0,
        step: 0.5,
        default: "6",
      }),
      number(
        "spike.high_price_threshold",
        "High price threshold",
        "Forecast sell price that counts as spike-worthy even without an Amber flag.",
        { unit: "$/kWh", min: 0, max: 20, step: 0.1, default: "1" },
      ),
      number(
        "spike.reserve_penalty_per_kwh",
        "Reserve penalty",
        "Softness of the reserve: cost per kWh per hour spent below it. Below true " +
          "spike value but above normal arbitrage margin, so only genuinely better " +
          "opportunities break the reserve.",
        { unit: "$/kWh", min: 0, max: 20, step: 0.1, default: "0.5" },
      ),
      number(
        "spike.discharge_kw",
        "Spike discharge cap",
        "Discharge limit while a CONFIRMED spike is active (current interval only) — " +
          "lets a wear-conscious everyday limit be exceeded for the rare high-value " +
          "hours. Set to your inverter's true limit; 0 disables. Extra power only " +
          "reaches the grid if the export limit allows it.",
        { unit: "kW", min: 0, max: 100, step: 0.5, default: "0" },
      ),
    ],
  },
];

/** Flat list of every field spec, for lookups by path. */
export const ALL_FIELDS: FieldSpec[] = SECTIONS.flatMap((s) => s.fields);

export function getPath(doc: Record<string, unknown> | null, path: string): unknown {
  let node: unknown = doc;
  for (const key of path.split(".")) {
    if (node == null || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[key];
  }
  return node;
}

export function setPath(doc: Record<string, unknown>, path: string, value: unknown): void {
  const keys = path.split(".");
  let node = doc;
  for (const key of keys.slice(0, -1)) {
    node = (node[key] ??= {}) as Record<string, unknown>;
  }
  node[keys[keys.length - 1]!] = value;
}
