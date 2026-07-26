import { ChevronRight } from "lucide-react";
import { type ReactNode, useState, useSyncExternalStore } from "react";
import type { Explanation, PlanResponse } from "./api";
import type { Row } from "./charts";
import { Popover, PopoverContent, PopoverTrigger } from "./components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "./components/ui/tooltip";
import { ACTION_COLORS, fmtTime, SERIES } from "./theme";

// Hover tooltips never open on touch screens — swap in a tap-to-open
// popover (styled like the tooltip) when the device can't hover.
const HOVER_NONE = window.matchMedia("(hover: none)");

function useTouchUI(): boolean {
  return useSyncExternalStore(
    (notify) => {
      HOVER_NONE.addEventListener("change", notify);
      return () => HOVER_NONE.removeEventListener("change", notify);
    },
    () => HOVER_NONE.matches,
  );
}

const HORIZON_COST_HELP =
  "Expected net cash flow at the meter over the plan horizon: planned grid " +
  "imports at forecast buy prices minus exports at forecast sell prices. " +
  "Negative = net earnings. Excludes battery wear cost and the value of " +
  "energy still stored at the horizon end, so a plan that ends with a full " +
  "battery looks 'worse' than one that sold everything.";

const METER_HELP =
  "Net cash across your grid meter this interval: energy imported at the buy " +
  "price minus energy exported at the feed-in price. Excludes battery wear " +
  "and the value of energy moved in or out of the battery, so it can read " +
  "$0.00 while the battery is busy charging from solar. The per-interval " +
  "sibling of the Horizon cost tile.";

const HOLD_VALUE_HELP =
  "What Numbat reckons a kWh still in the battery at the end of its 36-hour " +
  "horizon is worth to you — roughly what it would cost to buy that energy " +
  "back later (the cheapest upcoming import price, plus charging losses). It " +
  "stops the plan from draining the battery just because the horizon ends, " +
  "and it's the break-even the current feed-in price is weighed against " +
  "before selling: sell above it, hold below it.";

const ACTION_LABEL: Record<string, string> = {
  charge: "charging",
  discharge: "discharging",
  no_charge: "no charge",
  idle: "idle",
  curtail: "curtailing",
};

const ACTION_SUB: Record<string, string> = {
  charge: "charging from the grid",
  discharge: "exporting stored energy",
  no_charge: "self-consumption, charging blocked",
  idle: "self-consumption",
  curtail: "export capped — negative feed-in",
};

function HelpBadge({ label, help }: { label: string; help: string }) {
  const touch = useTouchUI();
  const trigger = (
    <button
      type="button"
      aria-label={`About ${label}`}
      className="ml-1.5 inline-block size-[13px] cursor-help rounded-full border border-muted-foreground/50 text-center text-[9px] leading-3 normal-case"
    >
      ?
    </button>
  );
  if (touch) {
    return (
      <Popover>
        <PopoverTrigger asChild>{trigger}</PopoverTrigger>
        <PopoverContent
          side="bottom"
          className="w-fit max-w-72 border-none bg-foreground px-3 py-1.5 text-xs text-background"
        >
          {help}
        </PopoverContent>
      </Popover>
    );
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>{trigger}</TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-72">
        {help}
      </TooltipContent>
    </Tooltip>
  );
}

/** Action-now hero card: the optimiser's current call at a glance, with an
 * expandable "More info" panel that narrates the numbers behind it plus the
 * plan's diagnostics (computed at, solver status, solve time). */
export function Hero({
  rows,
  explanation,
  plan,
}: {
  rows: Row[];
  explanation?: Explanation | null;
  plan?: PlanResponse;
}) {
  const step0 = rows[0];
  if (!step0) return null;
  const forced = step0.action === "charge" || step0.action === "discharge";
  const setpoint = forced
    ? `${step0.battery > 0 ? "+" : "−"}${Math.abs(step0.battery).toFixed(1)} kW`
    : "—";
  return (
    <div className="shadow-card flex flex-col rounded-lg border border-border bg-card px-[22px] py-[18px]">
      <div className="flex items-center justify-between gap-5">
        <div>
          <div className="text-[11px] font-semibold tracking-[.09em] text-muted-foreground uppercase">
            Action now
          </div>
          <div
            className="mt-1.5 font-mono text-[32px] leading-tight font-semibold capitalize"
            style={{ color: ACTION_COLORS[step0.action] ?? "var(--action)" }}
          >
            {ACTION_LABEL[step0.action] ?? step0.action.replace("_", " ")}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {fmtTime(step0.t)} – {fmtTime(step0.end)} · {ACTION_SUB[step0.action] ?? ""}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[11px] font-semibold tracking-[.09em] text-muted-foreground uppercase">
            Battery setpoint
          </div>
          <div className="mt-1.5 font-mono text-[28px] leading-tight font-semibold text-foreground">
            {setpoint}
          </div>
        </div>
      </div>
      {explanation && <MoreInfo explanation={explanation} plan={plan} />}
    </div>
  );
}

const money = (x: number) => `${x < 0 ? "−" : ""}$${Math.abs(x).toFixed(2)}`;
const kw = (x: number) => `${x.toFixed(1)} kW`;

function batteryText(x: number): { value: string; sub?: string } {
  if (Math.abs(x) < 0.05) return { value: "idle" };
  return { value: `${x > 0 ? "+" : "−"}${Math.abs(x).toFixed(1)} kW`, sub: x > 0 ? " charging" : " discharging" };
}

function gridMetric(v: Explanation["values"]): { value: string; sub?: string } {
  if (v.grid_export_kw > 0.05) return { value: kw(v.grid_export_kw), sub: " exporting" };
  if (v.grid_import_kw > 0.05) return { value: kw(v.grid_import_kw), sub: " importing" };
  return { value: "—" };
}

// Net cash at the grid meter this interval (see METER_HELP). interval_cost is
// signed: negative = export earnings, positive = import cost, ~0 = no flow.
function meterText(cost: number): { value: string; sub: string } {
  if (Math.abs(cost) < 0.005) return { value: "$0.00", sub: " net" };
  return cost < 0
    ? { value: `$${Math.abs(cost).toFixed(2)}`, sub: " earned" }
    : { value: `$${cost.toFixed(2)}`, sub: " cost" };
}

function Metric({
  label,
  value,
  sub,
  help,
}: {
  label: string;
  value: string;
  sub?: string;
  help?: string;
}) {
  return (
    <div>
      <dt className="text-[10px] font-semibold tracking-[.05em] text-muted-foreground uppercase">
        {label}
        {help && <HelpBadge label={label} help={help} />}
      </dt>
      <dd className="mt-0.5 font-mono text-[13px] text-foreground">
        {value}
        {sub && <span className="text-muted-foreground">{sub}</span>}
      </dd>
    </div>
  );
}

function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
      {children}
    </span>
  );
}

function MoreInfo({ explanation, plan }: { explanation: Explanation; plan?: PlanResponse }) {
  const [open, setOpen] = useState(false);
  const { reason, values: v, context: c, levers: l, stale } = explanation;
  const bat = batteryText(v.battery_kw);
  const grid = gridMetric(v);
  const meter = meterText(v.interval_cost);
  const chips = [
    l?.spike_reserve && (
      <Chip key="reserve">spike reserve {Math.round(l.spike_reserve.kwh)} kWh</Chip>
    ),
    l?.daily_target && <Chip key="target">daily charge target</Chip>,
    l?.live_spike && <Chip key="spike">spike live</Chip>,
    l?.prices_estimated && <Chip key="estimate">price still an estimate</Chip>,
    stale && <Chip key="stale">reusing previous plan</Chip>,
  ].filter(Boolean);
  const socSub =
    v.soc_start_pct != null && v.soc_end_pct != null
      ? ` ${v.soc_start_pct}→${v.soc_end_pct}%`
      : undefined;
  return (
    <div className="mt-3.5 border-t border-border pt-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full cursor-pointer items-center gap-1.5 text-left text-[13px] font-medium text-muted-foreground hover:text-foreground"
      >
        <ChevronRight className={`size-3.5 transition-transform ${open ? "rotate-90" : ""}`} />
        More info
      </button>
      {open && (
        <div className="mt-3 space-y-3">
          <p className="text-[13px] leading-relaxed text-foreground">{reason}</p>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-3">
            <Metric label="Buy" value={money(v.buy)} sub="/kWh" />
            <Metric label="Feed-in" value={money(v.sell)} sub="/kWh" />
            <Metric label="Solar" value={kw(v.pv_kw)} />
            <Metric label="House load" value={kw(v.load_kw)} />
            <Metric label="Battery" value={bat.value} sub={bat.sub} />
            <Metric label="Grid" value={grid.value} sub={grid.sub} />
            <Metric
              label="SoC"
              value={`${v.soc_start_kwh.toFixed(1)}→${v.soc_end_kwh.toFixed(1)} kWh`}
              sub={socSub}
            />
            <Metric label="Meter" value={meter.value} sub={meter.sub} help={METER_HELP} />
            {!stale && c?.hold_value != null && (
              <Metric
                label="Hold value"
                value={money(c.hold_value)}
                sub="/kWh"
                help={HOLD_VALUE_HELP}
              />
            )}
          </dl>
          {chips.length > 0 && (
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">{chips}</div>
          )}
          {plan && (
            // Plan-level diagnostics (vs the per-interval numbers above) —
            // moved here from the app bar, which also serves test mode.
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 border-t border-border pt-3 sm:grid-cols-3">
              <Metric label="Computed" value={fmtTime(Date.parse(plan.computed_at))} />
              <Metric label="Solver" value={plan.solver_status} />
              <Metric label="Solve time" value={`${Math.round(plan.solve_ms)} ms`} />
            </dl>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  help,
}: {
  label: string;
  value: ReactNode;
  sub: string;
  help?: string;
}) {
  return (
    <div className="shadow-card rounded-lg border border-border bg-card px-4 py-3.5">
      <div className="text-[10px] font-semibold tracking-[.06em] text-muted-foreground uppercase">
        {label}
        {help && <HelpBadge label={label} help={help} />}
      </div>
      <div className="mt-2 font-mono text-xl font-semibold text-foreground">{value}</div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{sub}</div>
    </div>
  );
}

/** 4-up stat grid (collapses 2-up, then 1-up on narrow widths). */
export function Stats({ plan, rows }: { plan: PlanResponse; rows: Row[] }) {
  const step0 = rows[0];
  const last = rows[rows.length - 1];
  if (!step0 || !last) return null;
  const horizonH = Math.round((last.end - step0.t) / 3_600_000);
  const loadKwh = rows.reduce((sum, r) => sum + (r.load * (r.end - r.t)) / 3_600_000, 0);
  const cost = plan.objective_cost;

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <Stat
        label="Amber buy / sell"
        value={
          <>
            <span style={{ color: SERIES.buy }}>${step0.buy.toFixed(2)}</span>
            <span className="text-muted-foreground"> / </span>
            <span style={{ color: SERIES.sell }}>${step0.sell.toFixed(2)}</span>
          </>
        }
        sub={plan.meta.prices_estimated ? "this interval · unconfirmed" : "this interval"}
        help={
          plan.meta.prices_estimated
            ? "Amber hasn't confirmed this interval's price yet — the plan was " +
              "solved on the forecast value and re-solves the moment the " +
              "confirmed price lands."
            : undefined
        }
      />
      <Stat
        label="Horizon cost"
        value={`$${cost < 0 ? "−" : ""}${Math.abs(cost).toFixed(2)}`}
        sub={`net over ${horizonH} h`}
        help={HORIZON_COST_HELP}
      />
      <Stat label="Forecast load" value={loadKwh.toFixed(1)} sub={`kWh / ${horizonH} h`} />
    </div>
  );
}
