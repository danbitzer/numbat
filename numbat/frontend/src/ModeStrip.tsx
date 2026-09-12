import { useState } from "react";
import { Card, LegendRow, type Row, TooltipPanel } from "./charts";
import { useHoverT } from "./hover";
import { FLOW_FAMILY_LABEL, FLOW_SHORT, familyOf, type FlowFamily } from "./flowText";
import {
  cursorStroke,
  FLOW_COLORS,
  fmtDayTime,
  GUTTER,
  idleSegmentColor,
  RIGHT_MARGIN,
  useDark,
} from "./theme";

interface Segment {
  family: FlowFamily;
  flow: string; // the first flow of the run, for the tooltip
  capped: boolean; // export capped (negative feed-in)
  pvOff: boolean; // solar generation switched off (negative buy)
  startMs: number;
  endMs: number;
}

// Contiguous runs of the same family and the same modifiers: a
// negative-price hold splits from a plain hold so the pattern has an edge.
function mergeSegments(rows: Row[]): Segment[] {
  const out: Segment[] = [];
  for (const row of rows) {
    const family = familyOf(row.flow);
    const last = out[out.length - 1];
    if (
      last &&
      last.family === family &&
      last.capped === row.exportCapped &&
      last.pvOff === row.pvOff &&
      last.endMs === row.t
    ) {
      last.endMs = row.end;
    } else if (row.end > row.t) {
      out.push({
        family,
        flow: row.flow,
        capped: row.exportCapped,
        pvOff: row.pvOff,
        startMs: row.t,
        endMs: row.end,
      });
    }
  }
  return out;
}

const FAMILIES: FlowFamily[] = [
  "charging_from_grid",
  "selling_stored_energy",
  "selling_solar",
  "holding_back_solar",
  "running_on_grid",
  "self",
];

// The two modifiers as translucent patterns over the family colour: diagonal
// stripes for export capped, dots for solar off — layered when both apply.
const STRIPE = "repeating-linear-gradient(135deg, rgba(0,0,0,0.22) 0 3px, transparent 3px 7px)";
const DOTS = "radial-gradient(rgba(0,0,0,0.38) 1.2px, transparent 1.6px) 0 0 / 6px 6px";

function paint(base: string, capped: boolean, pvOff: boolean): string {
  const layers = [...(pvOff ? [DOTS] : []), ...(capped ? [STRIPE] : []), base];
  return layers.join(", ");
}

function modifierText(seg: { capped: boolean; pvOff: boolean }): string {
  const parts = [...(seg.capped ? ["export capped"] : []), ...(seg.pvOff ? ["solar off"] : [])];
  return parts.length ? ` · ${parts.join(" · ")}` : "";
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
  const locate = (clientX: number, el: HTMLElement) => {
    const box = el.getBoundingClientRect();
    const ms = t0 + ((clientX - box.left) / box.width) * span;
    const seg = segments.find((s) => ms >= s.startMs && ms < s.endMs);
    setLocal(seg ? { x: clientX - box.left, width: box.width, seg } : null);
  };

  return (
    <Card
      title="Planned mode"
      right={
        <LegendRow
          items={[
            ...FAMILIES.map((f) => ({
              label: FLOW_FAMILY_LABEL[f],
              color: f === "self" ? idleSegmentColor(dark) : FLOW_COLORS[f],
            })),
            { label: "export capped", color: paint(idleSegmentColor(dark), true, false) },
            { label: "solar off", color: paint(idleSegmentColor(dark), false, true) },
          ]}
        />
      }
    >
      <div style={{ paddingLeft: GUTTER, paddingRight: RIGHT_MARGIN }}>
        <div
          className="relative h-7"
          onMouseMove={(e) => locate(e.clientX, e.currentTarget)}
          // touch screens can't hover: a tap shows the same tooltip, which
          // clears itself since no mouseleave will
          onClick={(e) => {
            locate(e.clientX, e.currentTarget);
            window.setTimeout(() => setLocal(null), 4000);
          }}
          onMouseLeave={() => setLocal(null)}
        >
          <div className="absolute inset-0 overflow-hidden rounded-[7px] border border-border">
            {segments.map((seg) => {
              const base = seg.family === "self" ? idleSegmentColor(dark) : FLOW_COLORS[seg.family];
              return (
                <div
                  key={seg.startMs}
                  className="absolute top-0 bottom-0"
                  style={{
                    left: `${pct(seg.startMs)}%`,
                    width: `${pct(seg.endMs) - pct(seg.startMs)}%`,
                    background: paint(base, seg.capped, seg.pvOff),
                  }}
                />
              );
            })}
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
                  {FLOW_SHORT[local.seg.flow] ?? local.seg.flow.replace(/_/g, " ")}
                  {modifierText(local.seg)}
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
