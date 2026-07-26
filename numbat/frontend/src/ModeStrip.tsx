import { useState } from "react";
import { Card, LegendRow, type Row, TooltipPanel } from "./charts";
import { useHoverT } from "./hover";
import {
  ACTION_COLORS,
  cursorStroke,
  fmtDayTime,
  GUTTER,
  idleSegmentColor,
  RIGHT_MARGIN,
  useDark,
} from "./theme";

interface Segment {
  action: string;
  startMs: number;
  endMs: number;
}

function mergeSegments(rows: Row[]): Segment[] {
  const out: Segment[] = [];
  for (const row of rows) {
    const last = out[out.length - 1];
    if (last && last.action === row.action && last.endMs === row.t) {
      last.endMs = row.end;
    } else if (row.end > row.t) {
      out.push({ action: row.action, startMs: row.t, endMs: row.end });
    }
  }
  return out;
}

// Keep the local tooltip's center clamped this far from the strip edges so
// the translate(-50%) panel stays inside the card.
const TIP_EDGE_PX = 90;

/**
 * Custom timeline strip (Recharts has no rangeBar): colored segments per
 * contiguous action run, positioned by % of the shared time domain so it
 * aligns with the charts' plot areas (same gutter + right margin; see the
 * CHART_MARGIN invariant note in theme.ts). Shows its own hover tooltip and
 * the crosshair synced from the charts.
 */
export function ModeStrip({ rows, domain }: { rows: Row[]; domain: [number, number] }) {
  const dark = useDark();
  const hoverT = useHoverT();
  const [local, setLocal] = useState<{ x: number; width: number; seg: Segment } | null>(null);
  const [t0, tEnd] = domain;
  const span = tEnd - t0;
  const segments = mergeSegments(rows);
  const pct = (ms: number) => (100 * (ms - t0)) / span;

  const cursorPct = hoverT !== null && hoverT >= t0 && hoverT <= tEnd ? pct(hoverT) : null;

  return (
    <Card
      title="Planned mode"
      right={
        <LegendRow
          items={[
            { label: "charge", color: ACTION_COLORS.charge },
            { label: "discharge", color: ACTION_COLORS.discharge },
            { label: "no charge", color: ACTION_COLORS.no_charge },
            { label: "idle", color: idleSegmentColor(dark) },
            { label: "curtail", color: ACTION_COLORS.curtail },
          ]}
        />
      }
    >
      <div style={{ paddingLeft: GUTTER, paddingRight: RIGHT_MARGIN }}>
        <div
          className="relative h-7"
          onMouseMove={(e) => {
            const box = e.currentTarget.getBoundingClientRect();
            const frac = (e.clientX - box.left) / box.width;
            const ms = t0 + frac * span;
            const seg = segments.find((s) => ms >= s.startMs && ms < s.endMs);
            setLocal(seg ? { x: e.clientX - box.left, width: box.width, seg } : null);
          }}
          onMouseLeave={() => setLocal(null)}
        >
          <div className="absolute inset-0 overflow-hidden rounded-[7px] border border-border">
            {segments.map((seg) => (
              <div
                key={seg.startMs}
                className="absolute top-0 bottom-0"
                style={{
                  left: `${pct(seg.startMs)}%`,
                  width: `${pct(seg.endMs) - pct(seg.startMs)}%`,
                  background:
                    seg.action === "idle"
                      ? idleSegmentColor(dark)
                      : (ACTION_COLORS[seg.action as keyof typeof ACTION_COLORS] ?? "#98a1ab"),
                }}
              />
            ))}
          </div>
          {cursorPct !== null && (
            <div
              className="pointer-events-none absolute top-0 bottom-0 border-l border-dashed"
              style={{ left: `${cursorPct}%`, borderColor: cursorStroke(dark) }}
            />
          )}
          {local && (
            <div
              className="pointer-events-none absolute -top-1 z-10"
              style={{
                left: Math.min(Math.max(local.x, TIP_EDGE_PX), local.width - TIP_EDGE_PX),
                transform: "translate(-50%, -100%)",
              }}
            >
              <TooltipPanel>
                <span className="font-semibold whitespace-nowrap">
                  {local.seg.action.replace("_", " ")}
                </span>
                <span className="text-muted-foreground whitespace-nowrap">
                  {" "}
                  {fmtDayTime(local.seg.startMs)} → {fmtDayTime(local.seg.endMs)}
                </span>
              </TooltipPanel>
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
