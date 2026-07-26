import type { ReactNode } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Action } from "./api";
import {
  CHART_HEIGHT,
  CHART_MARGIN,
  cursorStroke,
  fmtDayTime,
  fmtTime,
  GUTTER,
  gridStroke,
  SERIES,
  SERIES_FILL,
  useDark,
} from "./theme";
import { setHoverT } from "./hover";

// One parsed interval — timestamps are parsed exactly once, in App, and every
// consumer (charts, tiles, mode strip) works from these numbers.
export interface Row {
  t: number; // interval start, epoch ms
  end: number; // interval end, epoch ms
  action: Action;
  buy: number;
  sell: number;
  pv: number;
  load: number;
  battery: number; // +charge / −discharge
  gridImport: number;
  gridExport: number; // plotted negative (power leaving the house)
  soc: number; // kWh at interval end
}

/** Hourly-aligned ticks every `stepHours`, matching the plan's local timezone. */
function makeTicks(t0: number, tEnd: number, stepHours = 2): number[] {
  const first = new Date(t0);
  first.setMinutes(0, 0, 0);
  while (first.getTime() < t0) first.setHours(first.getHours() + 1);
  const ticks: number[] = [];
  for (const d = first; d.getTime() <= tEnd; d.setHours(d.getHours() + stepHours)) {
    ticks.push(d.getTime());
  }
  return ticks;
}

export function LegendRow({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {items.map(({ label, color }) => (
        <span key={label} className="inline-flex items-center gap-1.5">
          <span className="size-[9px] rounded-[2px]" style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}

type TipPayload = { name?: string | number; value?: number | string; color?: string }[];

/** Shared tooltip chrome — used by the chart tooltips AND the mode strip's,
 * so the two can't drift apart visually. */
export function TooltipPanel({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-sm">
      {children}
    </div>
  );
}

function ChartTip({
  active,
  payload,
  label,
  format,
}: {
  active?: boolean;
  payload?: TipPayload;
  label?: number | string;
  format: (name: string, value: number) => string;
}) {
  if (!active || !payload?.length || typeof label !== "number") return null;
  return (
    <TooltipPanel>
      <div className="mb-1 font-semibold">{fmtDayTime(label)}</div>
      {payload.map((p) =>
        typeof p.value === "number" && typeof p.name === "string" ? (
          <div key={p.name} className="flex items-center gap-1.5">
            <span className="size-2 rounded-full" style={{ background: p.color }} />
            <span className="text-muted-foreground">{p.name}:</span>
            <span className="font-semibold">{format(p.name, p.value)}</span>
          </div>
        ) : null,
      )}
    </TooltipPanel>
  );
}

export function Card({
  title,
  right,
  children,
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="shadow-card rounded-lg border border-border bg-card px-[18px] pt-[18px] pb-3.5">
      <div className="mb-3.5 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        {right}
      </div>
      {children}
    </div>
  );
}

interface HemChartProps {
  data: Row[];
  domain: [number, number];
  height?: number;
  yTickFormat: (v: number) => string;
  yDomain?: [number, number];
  format: (name: string, value: number) => string;
  children: ReactNode; // series + reference elements
}

/** Shared chart frame: fixed gutter, hourly ticks, synced tooltip + hover. */
export function HemChart({
  data,
  domain,
  height = CHART_HEIGHT,
  yTickFormat,
  yDomain,
  format,
  children,
}: HemChartProps) {
  const dark = useDark();
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart
        data={data}
        margin={CHART_MARGIN}
        syncId="numbat"
        onMouseMove={(state) => {
          const label = (state as { activeLabel?: number | string }).activeLabel;
          setHoverT(typeof label === "number" ? label : null);
        }}
        onMouseLeave={() => setHoverT(null)}
      >
        <CartesianGrid stroke={gridStroke(dark)} vertical={false} />
        <XAxis
          dataKey="t"
          type="number"
          domain={domain}
          ticks={makeTicks(domain[0], domain[1])}
          tickFormatter={fmtTime}
          tick={{ fontSize: 11, fontFamily: MONO, fill: mutedTick(dark) }}
          tickLine={false}
          axisLine={{ stroke: gridStroke(dark) }}
        />
        <YAxis
          width={GUTTER}
          domain={yDomain}
          tickFormatter={yTickFormat}
          tick={{ fontSize: 11, fontFamily: MONO, fill: mutedTick(dark) }}
          tickLine={false}
          axisLine={false}
        />
        <Tooltip
          content={<ChartTip format={format} />}
          isAnimationActive={false}
          cursor={{ stroke: cursorStroke(dark), strokeDasharray: "4 3" }}
        />
        {children}
      </ComposedChart>
    </ResponsiveContainer>
  );
}

const MONO = "ui-monospace, SFMono-Regular, Menlo, monospace";
const mutedTick = (dark: boolean) => (dark ? "#9b9b9b" : "#69727e");

const kw = (_: string, v: number) => `${v.toFixed(2)} kW`;
const dollars = (_: string, v: number) => `$${v.toFixed(3)}`;

export function PricesChart({
  rows,
  domain,
  forecastEnd,
}: {
  rows: Row[];
  domain: [number, number];
  forecastEnd: number | null;
}) {
  const dark = useDark();
  const padded = forecastEnd !== null && forecastEnd < domain[1];
  return (
    <Card
      title="Prices $/kWh (buy / sell)"
      right={
        <LegendRow
          items={[
            { label: "buy", color: SERIES.buy },
            { label: "sell", color: SERIES.sell },
          ]}
        />
      }
    >
      <HemChart data={rows} domain={domain} yTickFormat={(v) => v.toFixed(2)} format={dollars}>
        {padded && (
          <ReferenceArea
            x1={forecastEnd}
            x2={domain[1]}
            fill={dark ? "#3a3a40" : "#e9e9ee"}
            fillOpacity={0.45}
            label={{
              value: "forecast padded",
              angle: -90,
              // vertically centered, hugging the band's left edge
              position: "insideLeft",
              offset: 8,
              fontSize: 10,
              fill: dark ? "#9a9aa2" : "#6e6e73",
            }}
          />
        )}
        <Line dataKey="buy" name="buy" type="stepAfter" stroke={SERIES.buy} strokeWidth={2.2} dot={false} isAnimationActive={false} />
        <Line dataKey="sell" name="sell" type="stepAfter" stroke={SERIES.sell} strokeWidth={2} dot={false} isAnimationActive={false} />
      </HemChart>
    </Card>
  );
}

export function ForecastChart({
  rows,
  domain,
  info,
}: {
  rows: Row[];
  domain: [number, number];
  info?: string | null;
}) {
  return (
    <Card
      title="Forecast kW (PV / load)"
      right={
        <LegendRow
          items={[
            { label: "PV", color: SERIES.pv },
            { label: "load", color: SERIES.load },
          ]}
        />
      }
    >
      {info && (
        <div className="-mt-2.5 mb-2.5 text-xs text-muted-foreground">{info}</div>
      )}
      <HemChart data={rows} domain={domain} yTickFormat={(v) => v.toFixed(0)} format={kw}>
        <Area dataKey="pv" name="PV" type="stepAfter" stroke={SERIES.pv} strokeWidth={2} fill={SERIES_FILL.pv} fillOpacity={1} isAnimationActive={false} />
        <Line dataKey="load" name="load" type="stepAfter" stroke={SERIES.load} strokeWidth={2} dot={false} isAnimationActive={false} />
      </HemChart>
    </Card>
  );
}

export function BatteryChart({ rows, domain }: { rows: Row[]; domain: [number, number] }) {
  return (
    <Card
      title="Planned battery power kW (+charge / −discharge) & grid flows"
      right={
        <LegendRow
          items={[
            { label: "battery", color: SERIES.battery },
            { label: "import", color: SERIES.gridImport },
            { label: "export", color: SERIES.gridExport },
          ]}
        />
      }
    >
      <HemChart data={rows} domain={domain} yTickFormat={(v) => v.toFixed(0)} format={kw}>
        <Area dataKey="gridExport" name="export" type="stepAfter" stroke={SERIES.gridExport} strokeWidth={1.8} fill={SERIES_FILL.gridExport} fillOpacity={1} isAnimationActive={false} />
        <Area dataKey="gridImport" name="import" type="stepAfter" stroke={SERIES.gridImport} strokeWidth={1.8} fill={SERIES_FILL.gridImport} fillOpacity={1} isAnimationActive={false} />
        <Area dataKey="battery" name="battery" type="stepAfter" stroke={SERIES.battery} strokeWidth={2.2} fill={SERIES_FILL.battery} fillOpacity={1} isAnimationActive={false} />
      </HemChart>
    </Card>
  );
}

export function SocChart({
  rows,
  domain,
  capacity,
}: {
  rows: Row[];
  domain: [number, number];
  capacity: number | null;
}) {
  const format = (_: string, v: number) =>
    capacity ? `${v.toFixed(1)} kWh · ${((100 * v) / capacity).toFixed(0)}%` : `${v.toFixed(1)} kWh`;
  return (
    <Card title="Planned state of charge kWh">
      <HemChart
        data={rows}
        domain={domain}
        yTickFormat={(v) => v.toFixed(0)}
        yDomain={capacity ? [0, capacity] : undefined}
        format={format}
      >
        <Area dataKey="soc" name="SoC" type="stepAfter" stroke={SERIES.battery} strokeWidth={2.2} fill={SERIES_FILL.soc} fillOpacity={1} isAnimationActive={false} />
      </HemChart>
    </Card>
  );
}
