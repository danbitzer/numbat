import { useSyncExternalStore } from "react";
import type { Action } from "./api";

// Design 1A ("HA Cards") palette. Actions: charge = HA-blue accent,
// discharge = purple action accent, idle = neutral segment grey. no_charge
// and curtail aren't in the handoff's four-way legend; green (the design's
// export colour — no_charge usually means PV surplus exporting instead of
// charging) and amber (export held back) extend it in the same family.
export const ACTION_COLORS: Record<Action, string> = {
  charge: "#3f7fd0",
  discharge: "#8a52c9",
  idle: "var(--seg-idle)",
  no_charge: "#2fae7a",
  curtail: "#efa63c",
};

// Series colours are theme-independent per the handoff.
export const SERIES = {
  buy: "#e0563f",
  sell: "#2fae7a",
  pv: "#efa63c",
  load: "#98a1ab",
  battery: "#3f7fe0",
  gridImport: "#e0563f",
  gridExport: "#2fae7a",
} as const;

// Translucent area fills under stepped series (handoff MiniChart spec).
export const SERIES_FILL = {
  pv: "rgba(239,166,60,.16)",
  battery: "rgba(63,127,224,.18)",
  gridImport: "rgba(224,86,63,.14)",
  gridExport: "rgba(47,174,122,.16)",
  soc: "rgba(63,127,224,.14)",
} as const;

// Shared fixed y-axis gutter + right margin so every chart's plot area (and
// the CSS-positioned mode strip) spans the identical x range. The strip only
// knows GUTTER and RIGHT_MARGIN, so CHART_MARGIN.left MUST stay 0 and the
// YAxis width MUST stay GUTTER — a Recharts plot's left edge is
// margin.left + axis width, and the strip would silently drift otherwise.
export const GUTTER = 36;
export const RIGHT_MARGIN = 16;
export const CHART_MARGIN = { top: 8, right: RIGHT_MARGIN, bottom: 0, left: 0 };
export const CHART_HEIGHT = 200;

// Theme preference: "system" follows prefers-color-scheme, "light"/"dark"
// force it. Stored per browser (localStorage) the way other HA add-ons do it
// — the ingress iframe has no way to read the HA theme. The RESOLVED theme
// is stamped on <html data-theme> (index.html stamps it pre-paint, this
// store keeps it current), which is what index.css keys off.
export type ThemePref = "light" | "dark" | "system";

const STORAGE_KEY = "numbat-theme";
const query = window.matchMedia("(prefers-color-scheme: dark)");
const listeners = new Set<() => void>();

function storedPref(): ThemePref {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

let pref: ThemePref = storedPref();

function resolvedDark(): boolean {
  return pref === "dark" || (pref === "system" && query.matches);
}

function applyTheme(): void {
  document.documentElement.dataset.theme = resolvedDark() ? "dark" : "light";
  for (const notify of listeners) notify();
}

query.addEventListener("change", applyTheme);

export function setThemePref(next: ThemePref): void {
  pref = next;
  try {
    if (next === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // storage unavailable (private mode): still applies for this page load
  }
  applyTheme();
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

export function useThemePref(): ThemePref {
  return useSyncExternalStore(subscribe, () => pref);
}

export function useDark(): boolean {
  return useSyncExternalStore(subscribe, resolvedDark);
}

// SVG attributes can't resolve CSS variables, so chart strokes restate the
// index.css palette (--chart-grid / --muted-foreground values) per theme —
// keep in sync.
export function gridStroke(dark: boolean): string {
  return dark ? "#2a2a2a" : "#eceff3";
}

export function cursorStroke(dark: boolean): string {
  return dark ? "#9b9b9b" : "#69727e";
}

export function idleSegmentColor(dark: boolean): string {
  return dark ? "#3a3a3a" : "#d6dbe1";
}

export function fmtDayTime(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtTime(ms: number): string {
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
