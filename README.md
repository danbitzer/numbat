# Numbat — Energy Optimizer

**Numbat** — the **NUM**erical **BAT**tery optimizer for Home Assistant.

A Home Assistant add-on that optimizes home battery charge/discharge and
solar-export decisions against Amber Electric's 5-minute wholesale pricing.
Every 5 minutes it re-solves a mixed-integer linear program (MILP) over the
next ~36 hours and publishes what the battery should do *right now*.

Numbat is a **recommendation engine**. It never touches your inverter: it
publishes sensors, and actuation happens through a Home Assistant automation
you own (built from a shipped blueprint, with a heartbeat failsafe). That
makes it inverter-agnostic — anything HA can control can follow the plan.

**[→ Setup guide from a fresh HA install](docs/SETUP.md)** ·
**[→ Add-on docs / option reference](numbat/DOCS.md)**

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/dashboard-dark.png">
  <img alt="The Numbat dashboard: the current action and battery setpoint, live buy/sell prices, and the planned prices, PV/load forecast, mode, battery power and state of charge over the 36 h horizon" src="docs/screenshots/dashboard-light.png">
</picture>

*The ingress dashboard (demo data), mid-way through selling into an evening
price peak.*

## Inputs

All via existing HA integrations — no glue automations needed:

- **Prices**: [Amber Express](https://github.com/hass-energy/amber-express) in
  advanced-price mode — Numbat parses its `forecast` attribute (Amber's own
  SmartShift prediction) and the live price-spike binary sensor. The core
  `amberelectric` integration is not supported.
- **Solar forecast**: [Open-Meteo Solar Forecast](https://github.com/rany2/ha-open-meteo-solar-forecast)
  (`watts` attribute, 15-min resolution).
- **Battery**: any integration exposing SoC and battery power, e.g. Sungrow
  SHx via the [mkaiser Modbus package](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant).
- **Load**: learned daily from your actual consumption — hour-of-day averages
  from months of long-term statistics of a house-load sensor, plus an
  optional learned temperature response (kW per degree of cooling/heating)
  applied to the forecast temps from any hourly `weather.*` entity. No load
  sensor → Numbat plans with zero load and warns on the dashboard.

## How the optimizer works

Each cycle: **gather** entity states → **normalize** onto a shared time grid →
**solve** the MILP → **publish** the plan. The grid follows the forecast's
native boundaries (5-min intervals near-term, 30-min beyond, a fractional
first step from *now*), so no forecast information is smeared by resampling.

Per step, the decision variables are battery charge/discharge power, grid
import/export, and PV curtailment, subject to:

- power balance (PV + discharge + import = load + charge + export)
- SoC dynamics with charge/discharge efficiency, SoC min/max bounds
- battery power limits, grid connection import/export limits
- no simultaneous charge & discharge (the binary variables that make it a MILP)

The objective minimizes the horizon energy bill plus a **battery wear cost**
per discharged kWh, minus a **terminal value** on energy left in the battery at
the horizon (default: median buy price × efficiency − wear, so the battery
isn't dumped at any positive price just because the horizon ends). Spike
capture, curtailment under negative feed-in, and charging on negative prices
all fall out of the economics rather than hand-written rules.

Because Amber forecasts are routinely wrong about spikes, several layers keep
the plan honest:

- **Spike reserve**: while a high forecast price sits within the lookahead
  window, a soft SoC floor holds energy ready to sell — soft, so a genuinely
  better opportunity can still break it. It triggers on raw forecast prices.
- **Forecast haircut**: above-median sell prices beyond ~6h are discounted
  toward the median in the objective, so phantom distant spikes don't distort
  near-term decisions.
- **Event-triggered re-solve**: a WebSocket watcher re-solves within seconds
  of ANY live price change — including Amber confirming an interval's
  estimated price — so the plan always reflects the real price. A confirmed
  spike gets a full-power discharge decision immediately, optionally at a
  raised spike-only discharge cap (`spike.discharge_kw`).
- **Never grid-charge during a confirmed spike**, as a hard guard on top of
  the economics.
- **Hysteresis**: the current action only switches if the switch improves the
  full horizon objective by more than a threshold (solved pin-and-compare), so
  near-degenerate solutions don't chatter the inverter.
- **Fallback**: solver failure reuses the previous plan shifted forward;
  stale inputs degrade to idle recommendations, never silent garbage.

## Outputs

Published every cycle (REST sensors): `sensor.numbat_action`
(charge/discharge/idle/curtail), `sensor.numbat_power_setpoint` (signed kW, with
`power_w` attribute), `sensor.numbat_soc_target`, `sensor.numbat_horizon_cost`,
and `sensor.numbat_status` (heartbeat).
An ingress dashboard charts the plan: prices, PV/load forecasts, planned
battery power, and the SoC trajectory.

Actuation = your automation from
[blueprints/numbat_actuator.yaml](blueprints/numbat_actuator.yaml): it maps
action + setpoint onto your inverter's controls, and reverts to
self-consumption when Numbat's heartbeat goes stale. See
[numbat/DOCS.md](numbat/DOCS.md) for a complete Sungrow example.

## Under the hood

| | |
|---|---|
| Optimization | [CVXPY](https://www.cvxpy.org/) (`cvxpy-base`) + [HiGHS](https://highs.dev/) (`highspy`) — ~70 binaries/solve, tens of ms |
| Numerics | numpy (no pandas; the time-grid resampler is ~50 lines) |
| HA I/O | aiohttp — REST for states/publishing, WebSocket for event-triggered re-solves |
| Config | pydantic + pydantic-settings — edited in the web UI's Settings view, persisted to a Numbat-owned `numbat-config.json` (`NUMBAT_*` env for connection/dev) |
| Dashboard | React 19 (+ React Compiler) + Recharts + Tailwind, built with Vite/Bun into a fully offline bundle; served by FastAPI + uvicorn behind HA ingress |
| Packaging | uv-locked deps; Debian-based image (cvxpy has no musl wheels); multi-arch (amd64/aarch64) prebuilt via GitHub Actions → GHCR |

Layout: the repo root is an HA add-on repository; the add-on and all Python
lives in [numbat/](numbat/) (`src/numbat/` — adapters, timegrid, optimizer, planner,
publisher, web; the React dashboard sources in `frontend/`), with the actuator
blueprint in
[blueprints/](blueprints/).

## Install (HA OS / Supervised)

Settings → Add-ons → Add-on store → ⋮ → Repositories → add this repo's URL,
then install **Numbat — Energy Optimizer**. Prebuilt images are pulled from GHCR
(maintainer note: after the first CI publish, the `numbat-amd64`/`numbat-aarch64`
packages must be set to public on GitHub or installs can't pull them).
Full walkthrough including the input integrations: [docs/SETUP.md](docs/SETUP.md).

## Development (no Home Assistant OS required)

Run directly against any HA instance with a long-lived access token:

```sh
cd numbat
uv sync
NUMBAT_HA_URL=http://homeassistant.local:8123 \
NUMBAT_HA_TOKEN=<long-lived token> \
uv run python -m numbat
```

or via Docker, from the **repo root**: `docker compose -f docker-compose.dev.yml up --build`
(reads `NUMBAT_HA_URL`/`NUMBAT_HA_TOKEN` from your shell environment or a repo-root
`.env`; note the standalone run above uses `numbat/.env` instead).

The local timezone anchors the learned load buckets, the daily SoC target
and vacation end times. Under the Supervisor it comes from the `TZ` env var
automatically; in dev set `NUMBAT_TZ=Australia/Adelaide` (env or `numbat/.env` —
copy [numbat/.env.example](numbat/.env.example) to get started) or Numbat falls back
to UTC. The resolved zone is logged at startup.

Configure via the web UI at `http://localhost:8099` (Settings). Note the dev
server has **no authentication** — anyone on your LAN who can reach :8099 can
read and edit the config. Under the Supervisor this doesn't apply (ingress
only, HA-session-authenticated, no host port).

Tests: `cd numbat && uv run pytest`
