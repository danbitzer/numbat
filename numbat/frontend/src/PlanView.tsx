import type { PlanResponse } from "./api";
import { BatteryChart, ForecastChart, PricesChart, type Row, SocChart } from "./charts";
import { flowOf } from "./flowText";
import { ModeStrip } from "./ModeStrip";
import { RangeBar, useRangeHours } from "./RangeBar";
import { Hero, Stats } from "./Tiles";

/** The shared plan render — hero, stat row, and all charts — used by both the
 * live Dashboard and Test mode so a simulated plan looks exactly like a real
 * one. `info` is the optional load-forecast line under the forecast chart. */
export function PlanView({ plan, info }: { plan: PlanResponse; info?: string | null }) {
  const [rangeHours, setRangeHours] = useRangeHours();
  // Parse interval timestamps once; every child works from Row. (No useMemo:
  // the React Compiler memoizes these; Query's structural sharing keeps
  // identity stable on quiet polls.)
  const rows: Row[] = plan.intervals.map((iv) => ({
    t: Date.parse(iv.start),
    end: Date.parse(iv.end),
    action: iv.action,
    buy: iv.buy,
    sell: iv.sell,
    pv: iv.pv_kw,
    load: iv.load_kw,
    battery: iv.power_kw,
    gridImport: iv.grid_import_kw,
    gridExport: -iv.grid_export_kw,
    soc: iv.soc_end,
    flow: flowOf(iv.flow, iv.action),
    exportCapped: !!iv.export_capped,
    pvOff: !!iv.pv_off,
  }));

  const first = rows[0];
  const last = rows[rows.length - 1];
  if (!first || !last) {
    return <div className="p-6 text-center text-muted-foreground">plan is empty</div>;
  }
  // The charts and the mode strip show the chosen window from now; the
  // tiles above them keep the whole plan (the horizon cost is a horizon
  // number). Intervals are kept whole: the window ends on the boundary of
  // the last interval that starts inside it.
  const windowEnd = rangeHours ? Math.min(last.end, first.t + rangeHours * 3_600_000) : last.end;
  const shown = rows.filter((r) => r.t < windowEnd);
  // Step charts need a closing point at the final interval's END, or every
  // line stops one interval short of the axis edge (and of the mode strip).
  const lastShown = shown[shown.length - 1] ?? last;
  const chartRows: Row[] = [...shown, { ...lastShown, t: lastShown.end }];
  const domain: [number, number] = [first.t, lastShown.end];
  const fcEnd = plan.meta.price_forecast_end ? Date.parse(plan.meta.price_forecast_end) : null;

  return (
    <>
      <Hero rows={rows} explanation={plan.meta.explanation} plan={plan} />
      <Stats plan={plan} rows={rows} />
      <RangeBar hours={rangeHours} onChange={setRangeHours} />
      <PricesChart rows={chartRows} domain={domain} forecastEnd={fcEnd} />
      <ForecastChart rows={chartRows} domain={domain} info={info ?? null} />
      <ModeStrip rows={shown} domain={domain} />
      <BatteryChart rows={chartRows} domain={domain} />
      <SocChart rows={chartRows} domain={domain} capacity={plan.meta.capacity_kwh ?? null} />
    </>
  );
}
