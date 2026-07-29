// Regenerates the README dashboard screenshots (docs/screenshots/*.png).
//
// Serves the built dashboard (run via `bun run screenshot`, which builds
// first) with a mock /api on a random port, then captures it with headless
// Firefox in light and dark themes. The mock plan is pinned to a fixed
// instant so an unchanged dashboard re-captures to an unchanged image;
// times render in the machine's local timezone.
//
// Needs Firefox (FIREFOX_BIN to override the default macOS path). Safe to
// run while a real Firefox is open: -no-remote + a throwaway profile.
import { file } from "bun";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const FRONTEND = resolve(import.meta.dir, "..");
const REPO_ROOT = resolve(FRONTEND, "../..");
const DIST = resolve(FRONTEND, "../src/numbat/web/dist");
const OUT_DIR = join(REPO_ROOT, "docs", "screenshots");
const FIREFOX =
  process.env.FIREFOX_BIN ?? "/Applications/Firefox.app/Contents/MacOS/firefox";

if (!(await file(join(DIST, "index.html")).exists())) {
  console.error(`no build at ${DIST} — run this via \`bun run screenshot\``);
  process.exit(1);
}

// 17:00 Adelaide time: the money shot — discharging into the evening peak.
const T0 = new Date("2026-07-28T07:30:00Z");
const CAPACITY = 44.8;

/** Piecewise-linear curve over local hour-of-day. */
function curve(breaks: [number, number][], hod: number): number {
  for (let i = 1; i < breaks.length; i++) {
    const [h0, v0] = breaks[i - 1];
    const [h1, v1] = breaks[i];
    if (hod <= h1) return v0 + ((v1 - v0) * (hod - h0)) / (h1 - h0);
  }
  return breaks[breaks.length - 1][1];
}

const BUY: [number, number][] = [
  [0, 0.15], [2, 0.09], [4, 0.07], [6, 0.14], [7.5, 0.3], [9, 0.19],
  [11, 0.11], [14, 0.12], [16, 0.3], [16.5, 0.55], [17, 0.85], [18, 0.95],
  [19, 0.7], [20, 0.38], [22, 0.25], [24, 0.15],
];

function makePlan() {
  const intervals = [];
  let soc = 38.2;
  let objective = 0;
  for (let i = 0; i < 72; i++) {
    const start = new Date(T0.getTime() + i * 30 * 60000);
    const end = new Date(start.getTime() + 30 * 60000);
    const hod = start.getHours() + start.getMinutes() / 60;
    const buy = Math.round(curve(BUY, hod) * 1000) / 1000;
    const pv = hod > 7 && hod < 17.5 ? Math.round(80 * Math.sin(((hod - 7) / 10.5) * Math.PI)) / 10 : 0;
    let sell = Math.max(0.01, buy - 0.08);
    if (pv > 4) sell = Math.min(sell, 0.04); // midday solar glut
    sell = Math.round(sell * 1000) / 1000;
    const load = 0.5 + (hod >= 17 && hod < 21.5 ? 0.9 : 0) + (hod >= 6.5 && hod < 9 ? 0.4 : 0);

    let action = "idle";
    let power = 0;
    if (hod >= 17 && hod < 20) [action, power] = ["discharge", -8];
    else if (hod >= 2 && hod < 4) [action, power] = ["charge", 5]; // cheap grid top-up
    else if (hod >= 9 && hod < 15.5 && pv > 4.5) [action, power] = ["charge", 4]; // solar
    const soc_start = soc;
    soc = Math.min(CAPACITY * 0.95, Math.max(CAPACITY * 0.1, soc + (power > 0 ? power * 0.95 : power) * 0.5));

    const surplus = pv - load - Math.max(0, power) + Math.max(0, -power);
    const grid_export_kw = Math.round(Math.max(0, surplus) * 10) / 10;
    const grid_import_kw = Math.round(Math.max(0, -surplus) * 10) / 10;
    const interval_cost = Math.round((buy * grid_import_kw - sell * grid_export_kw) * 0.5 * 100) / 100;
    objective += interval_cost;
    intervals.push({
      start: start.toISOString(), end: end.toISOString(), action,
      power_kw: power, soc_start, soc_end: soc, buy, sell,
      pv_kw: pv, load_kw: load, grid_import_kw, grid_export_kw, interval_cost,
    });
  }
  const s0 = intervals[0];
  return {
    computed_at: T0.toISOString(),
    solver_status: "optimal",
    solve_ms: 42,
    objective_cost: Math.round(objective * 100) / 100,
    meta: {
      capacity_kwh: CAPACITY,
      load_forecast: "learned",
      load_forecast_info: {
        load_entity: "sensor.load_power", source: "long-term statistics",
        window_days: 30, temp_response: true, heat_kw_per_deg: 0.12,
        cool_kw_per_deg: 0.2, buffer: 0.1,
      },
      explanation: {
        reason:
          "Exporting stored energy — the $0.77/kWh feed-in price is the highest in the " +
          "forecast, well above the $0.21/kWh value of keeping it stored — so selling " +
          "now beats holding.",
        values: {
          buy: s0.buy, sell: s0.sell, pv_kw: s0.pv_kw, load_kw: s0.load_kw,
          soc_start_kwh: s0.soc_start, soc_end_kwh: s0.soc_end,
          soc_start_pct: Math.round((s0.soc_start / CAPACITY) * 1000) / 10,
          soc_end_pct: Math.round((s0.soc_end / CAPACITY) * 1000) / 10,
          battery_kw: s0.power_kw, grid_import_kw: s0.grid_import_kw,
          grid_export_kw: s0.grid_export_kw, interval_cost: s0.interval_cost,
        },
        context: { sell_rank: 1, buy_rank: 40, horizon_steps: 72, hold_value: 0.21, flat: false, hysteresis: false },
        levers: { spike_reserve: null, daily_target: false, live_spike: false, prices_estimated: false },
      },
    },
    intervals,
  };
}

const config = {
  configured: true,
  lifecycle: "running",
  config: { enabled: true, battery: { capacity_kwh: CAPACITY } },
};

const server = Bun.serve({
  // Loopback only: this must never be reachable from the LAN, however briefly.
  hostname: "127.0.0.1",
  port: 0,
  async fetch(req) {
    const url = new URL(req.url);
    const p = url.pathname;
    if (p === "/health")
      return Response.json({ healthy: true, lifecycle: "running", last_success: T0.toISOString(), last_error: "" });
    if (p === "/api/config") return Response.json(config);
    if (p === "/api/entities") return Response.json({ entities: [] });
    if (p === "/api/plan") return Response.json(makePlan());
    if (p === "/slow.png") {
      // Holds the window `load` event (which Firefox --screenshot waits for)
      // so React can mount, fetch the plan, and finish rendering first.
      await new Promise((r) => setTimeout(r, 3500));
      const px = Uint8Array.from(
        atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="),
        (c) => c.charCodeAt(0),
      );
      return new Response(px, { headers: { "content-type": "image/png" } });
    }
    if (p === "/" || p === "/index.html") {
      let html = await file(join(DIST, "index.html")).text();
      const theme = url.searchParams.get("theme") === "dark" ? "dark" : "light";
      html = html.replace(
        "<head>",
        `<head><script>try{localStorage.setItem('numbat-theme','${theme}')}catch(e){}</script>`,
      );
      html = html.replace(
        "</body>",
        '<img src="./slow.png" style="position:absolute;width:1px;height:1px;opacity:0" alt=""></body>',
      );
      return new Response(html, { headers: { "content-type": "text/html" } });
    }
    const f = file(join(DIST, p));
    if (await f.exists()) return new Response(f);
    return new Response("nope", { status: 404 });
  },
});

// Headless --screenshot captures exactly the window (the dashboard page
// doesn't scroll the body), and content height is ~1625px with this mock
// plan — 1650 frames the full dashboard with its natural bottom padding.
// If the dashboard gains a card, bump the height.
const SIZE = "1100,1650";

async function capture(theme: string, out: string) {
  const profile = mkdtempSync(join(tmpdir(), "numbat-shot-"));
  const proc = Bun.spawn(
    [FIREFOX, "-headless", "-no-remote", "--profile", profile,
     `--window-size=${SIZE}`, "--screenshot", out,
     `http://localhost:${server.port}/?theme=${theme}`],
    { stdout: "ignore", stderr: "ignore" },
  );
  const timeout = setTimeout(() => proc.kill(), 60000);
  await proc.exited;
  clearTimeout(timeout);
  rmSync(profile, { recursive: true, force: true });
  if (!(await file(out).exists())) throw new Error(`no screenshot produced for ${theme}`);
  console.log(`wrote ${out}`);
}

await capture("light", join(OUT_DIR, "dashboard-light.png"));
await capture("dark", join(OUT_DIR, "dashboard-dark.png"));
server.stop();
