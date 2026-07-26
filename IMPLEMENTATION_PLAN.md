# HEM — Home Energy Manager: Implementation Plan

## Status (2026-07-15 overnight session)

- **Phase 0–3 complete** (scaffold, ingestion, optimizer + dry-run publishing, backtester), 80 tests green, multi-arch image verified.
- **Phase 4 redesigned (2026-07-16, Dan's call)**: HEM never writes to the inverter. Actuation is a user-owned HA automation built from `blueprints/hem_actuator.yaml` (heartbeat failsafe built in), making HEM inverter-agnostic. The in-process `SungrowExecutor` was removed (git history has it). Remaining: backtest gate, then bench-test the actuator automation.
- **Phase 5 complete (2026-07-16)**: ingress dashboard shipped; `.github/workflows/build.yml` (test → `home-assistant/builder` matrix) publishes `ghcr.io/danbitzer/hem-{aarch64,amd64}` on pushes to main, and `config.yaml` has the `image:` key so Supervisor pulls instead of building on-device. One-time step after the first publish: make both GHCR packages public.
- **Live status**: Dan's dry-run loop is running against his HA (Tailscale), recording history to `hem/data/history/` for the backtest gate.
- **Load forecasting v2 (2026-07-16)**: optional `load_profile.source: history` learns the hourly baseline from recorder history of `entities.load_power` (time-weighted hour-of-day × weekday/weekend, configured profile as per-hour fallback, never fatal). Docs: `docs/SETUP.md` (fresh-install guide) + expanded README shipped.

## Context

Dan (Amber Electric customer, Sungrow hybrid inverter + battery via the mkaiser Modbus YAML package) wants a purpose-built home energy app that optimizes battery charge/discharge and solar-export decisions to profit from Amber's 5-minute wholesale pricing. Existing tools (EMHASS, Predbat) do MILP battery optimization but require significant glue (price-injection automations, cron loops, config sprawl); the decision is to build a tailored app that natively consumes the Amber, Open-Meteo Solar Forecast, and Sungrow entities with zero glue automations.

**Locked-in decisions:**
- Build from scratch (learning from EMHASS/Predbat designs)
- **HA Add-on** (Docker, talks to HA via Supervisor proxy) + standalone-Docker/local dev mode with a long-lived token
- **MILP rolling-horizon MPC** — CVXPY + HiGHS, 36 h horizon, re-solved every 5 min
- **v1 is dry-run**: publish the recommended plan as HA sensors only; inverter write-mode comes later behind a config switch

**Verified facts — corrected against Dan's live entity data (2026-07-15):**
- **Amber Express** (hass-energy/amber-express, HACS — Dan's price source): `sensor.amber_express_general_price` / `sensor.amber_express_feed_in_price`, state + `forecast` attribute in **$/kWh**. **Dan's site is a 5-minute site**: `forecast` entries are 5-min near-term (~40 min) then 30-min, extending ~1.5 days. The `forecast` values ARE Amber's advanced price prediction (per Dan's config) — **HEM parses `forecast`, not `detailedForecast`** (Dan's call; `detailedForecast` exists with spike_status/low/high bands but is recorder-excluded and redundant). **Feed-in arrives positive = export revenue** (matches HEM's internal convention — no sign flip). `binary_sensor.amber_express_price_spike` with `spike_status` attr (none/potential/spike) provides live spike state. Timestamps are local **+09:30 (Adelaide)** — half-hour UTC offset, still 30-min-grid aligned; everything converts to UTC in adapters. Fixtures: `hem/tests/fixtures/amber_express_*.yaml`.
- Amber core integration (fallback adapter, Phase 2+): current price sensors + Forecast sensors with a `forecasts` attribute (30-min dicts: `start_time`, `per_kwh`, `spot_per_kwh`, `spike_status`).
- **Open-Meteo Solar Forecast**: `sensor.home_energy_production_today` / `_tomorrow`; `watts` attribute = ISO local timestamp → instantaneous W at 15-min resolution (Dan's system peaks ~8.9 kW). Fixtures captured.
- Open-Meteo Solar Forecast (rany2/ha-open-meteo-solar-forecast): `energy_production_today`/`_tomorrow` sensors expose `watts`/`wh_period` attributes (ISO timestamp → W/Wh, 15-min resolution).
- mkaiser Sungrow package control entities: EMS mode select (Self-consumption/Forced/external EMS), forced charge/discharge command + power setpoint, min/max SoC, export power limit.
- Temperature forecast: any HA `weather.*` entity via the `weather.get_forecasts` service (needs WebSocket service-call-with-response).

---

## 1. Repo layout

The repo root is an HA **add-on repository**; the add-on lives in `hem/` (which is also the Docker build context). Move the existing `pyproject.toml`/`main.py` from the uv-init scaffold into it.

```
/Users/dan/Developer/hem/
├── repository.yaml                  # makes repo installable via HA "Add repository"
├── README.md
├── docker-compose.dev.yml           # standalone dev vs real HA + long-lived token
├── .github/workflows/build.yml      # (Phase 5) multi-arch prebuilt images
└── hem/                             # the add-on (slug: hem) = Docker build context
    ├── config.yaml                  # manifest: options schema, ingress, homeassistant_api, watchdog
    ├── build.yaml                   # per-arch base images (aarch64, amd64)
    ├── Dockerfile
    ├── DOCS.md
    ├── translations/en.yaml
    ├── pyproject.toml / uv.lock
    ├── src/hem/
    │   ├── __main__.py              # python -m hem
    │   ├── config.py                # pydantic: /data/options.json OR env for standalone
    │   ├── models.py                # Series, PriceForecast, BatteryState, Plan, PlanInterval, Action
    │   ├── timegrid.py              # TimeGrid + resampling (the shared normalization layer)
    │   ├── ha/client.py             # aiohttp REST + WS (supervisor proxy or direct URL/token)
    │   ├── ha/publisher.py          # POST /api/states sensor publishing
    │   ├── adapters/                # amber.py, solar.py, sungrow.py, weather.py
    │   ├── forecast/load.py         # LoadForecaster protocol + BaselineLoadForecaster
    │   ├── optimizer/model.py       # CVXPY MILP build/solve
    │   ├── optimizer/result.py      # solution → Plan
    │   ├── planner.py               # one cycle: gather → normalize → solve → hysteresis/fallback
    │   ├── recorder.py              # JSONL snapshots of inputs/plans to /data (feeds backtester)
    │   ├── backtest/                # sim.py, policies.py (baselines), cli.py
    │   ├── web/                     # FastAPI ingress app + static index.html + vendored apexcharts
    │   └── main.py                  # asyncio scheduler, health endpoint, heartbeat
    └── tests/                       # fixtures/ (recorded entity JSON) + unit tests
```

**Packaging gotcha (decides the Dockerfile):** HA's default Alpine bases are musl — cvxpy has no musllinux wheels and would compile from source. Use a Debian base (`python:3.13-slim-bookworm` is fine; add-ons need not use HA base images). `cvxpy-base`, `highspy`, `numpy` all ship manylinux wheels for amd64 + aarch64 → pure-wheel install.

**Dev mode:** `config.py` resolves auth: if `SUPERVISOR_TOKEN` env exists → `http://supervisor/core/api` / `ws://supervisor/core/websocket`; else `HEM_HA_URL` + `HEM_HA_TOKEN`. Day-to-day dev is just `uv run python -m hem` on the Mac against the real HA instance.

## 2. Architecture

All I/O is async (aiohttp); the core (timegrid, load forecaster, optimizer, simulator) is **pure sync Python on dataclasses/numpy** — this seam enables unit testing and offline backtesting.

Extension seams (Protocols): `PriceProvider.get_prices() -> PriceForecast`, `PvProvider.get_pv() -> Series`, `LoadForecaster.forecast(grid, temps) -> np.ndarray (kW/step)`, `Executor.apply(plan)`. **`AmberExpressAdapter` is the primary/default `PriceProvider`** (Dan's setup): parses `detailedForecast` from the general + feed-in price sensors — un-negates feed-in to our positive-revenue convention, carries per-interval `spike_status`, `estimate`, and the `advanced_price_predicted` low/predicted/high band into `PriceForecast`; reads `binary_sensor...._price_spike` for live spike state. `AmberCoreAdapter` (core integration's `forecasts` attribute) is the fallback impl chosen by config. A future learned load forecaster drops in behind `LoadForecaster`.

**Internal conventions (normalize once, in adapters, with fixture-locked tests):** prices $/kWh; feed-in positive = revenue; battery power positive = charging; all timestamps UTC.

### Time grid (mixed resolutions: Amber 5-min→30-min, Open-Meteo 15-min, 5-min loop)
**Data-driven grid** (revised once Dan's 5-min-site data arrived): grid boundaries come from the price forecast's own interval starts — 5-min steps near-term where Amber provides them, 30-min beyond — with a fractional first step (now → next forecast boundary) and 30-min padding out to the horizon when the forecast is shorter. Implemented in `hem/src/hem/timegrid.py` (`TimeGrid.build`, `resample_previous` for prices, time-weighted `resample_mean` for PV/load, `coverage` for staleness). Horizon default 36 h (~80 steps on a 5-min site). All UTC internally; local time only for load-profile hour lookup and UI.

### Load forecast (v1)
`BaselineLoadForecaster`: 24 hourly kW values × {weekday, weekend} from config + temp rules (`{when: temp_above|temp_below, threshold_c, add_kw}`) applied per step. Local hour-of-day lookup (DST-aware) on a UTC grid.

### Scheduler
Sleep to next 5-min wall-clock boundary **plus** an event-driven early re-solve when the Amber current-price entity changes (WS subscription, 10 s debounce) — catches spike announcements between ticks. Cycle timeout 60 s. `/health` returns 200 only if last successful cycle < 15 min → drives Supervisor `watchdog:`.

## 3. MILP formulation

Steps `t = 0..T−1`, duration `Δt[t]` h (fractional first step). Continuous vars ≥ 0: `pc` charge kW, `pd` discharge kW, `gi` import kW, `ge` export kW, `pv_u` PV used kW, `soc[0..T]` kWh. Binary: `y[t]` (1 = charging allowed).

```
Power balance:   pv_u[t] + pd[t] + gi[t] == load[t] + pc[t] + ge[t]
Curtailment:     0 <= pv_u[t] <= pv[t]
SoC dynamics:    soc[t+1] == soc[t] + (ηc·pc[t] − pd[t]/ηd)·Δt[t]
Bounds:          soc[0] == soc0 (clamped into bounds);  E·soc_min <= soc[t] <= E·soc_max
No simultaneous: pc[t] <= Pc·y[t] ;  pd[t] <= Pd·(1−y[t])
Grid limits:     gi[t] <= Gi ;  ge[t] <= Ge
Optional:        allow_grid_charge=false → pc[t] <= pv_u[t]

minimize  Σ (p_buy·gi − p_sell·ge)·Δt          # energy cost/revenue
        + c_wear · Σ pd·Δt                      # battery wear on discharge throughput
        + ε · Σ (pc + pd)·Δt                    # ε≈0.0005 anti-chatter tiebreak
        − v_T · soc[T]                          # terminal SoC value
```

- **Terminal value** `v_T = median(p_buy over horizon)·ηd − c_wear` (config-overridable) — prevents horizon-end battery drain without hard-coding a target SoC.
- No import/export-exclusivity binaries needed: clamp `p_sell = min(p_sell, p_buy − 0.001)` pre-model (true for Amber anyway).
- **Spikes and curtailment fall out naturally**: high `p_sell` → pre-charge then full-power export; negative `p_sell` → curtail (`pv_u < pv`); negative `p_buy` → grid-charge.
- SoC-depth-aware wear later via 2–3 stacked SoC layer vars with per-layer `c_wear_k` — config takes scalar-or-list now.
- Scale: ~72 binaries / ~430 continuous — HiGHS solves in well under 1 s. Use CVXPY Parameters + warm start for cheap 5-min re-solves.

### Spike strategy (maximize spike revenue, never buy into spikes)

Amber forecasts are unreliable, but spikes are where the money is. Layered approach:

1. **Optimize on `advanced_price_predicted.predicted`, not raw AEMO `per_kwh`** — Amber's own prediction (what SmartShift uses); AEMO forecasts over-predict spike duration by hours. Document setting Amber Express to advanced-price mode.
2. **Spike readiness reserve (the key hedge)**: when any interval within the next `spike_lookahead_hours` (default 4 h) has `spike_status == potential` (or `advanced_price_predicted.high` above a config threshold, default $1/kWh), add a soft constraint keeping `spike_reserve_kwh` (default ~50% capacity) in the battery: `soc[t] >= reserve` with a slack variable penalized at a price below true spike value but above normal arbitrage margin. The optimizer then only breaks the reserve for genuinely better opportunities. This monetizes spikes that forecasts under-call without betting everything on ones that never materialize.
3. **React within seconds when a spike confirms**: WS subscription on the current-price sensors and the `price_spike` binary sensor triggers an immediate re-solve (debounced 10 s); step 0 uses the live confirmed price, so a confirmed spike → full-power discharge in the same 30-s window. Amber Express's adaptive polling means confirmed prices land seconds after publication.
4. **Never import during spikes**: falls out of the MILP (spike `p_buy` makes `gi` ruinously expensive), plus a hard belt-and-braces rule in the planner: if live `spike_status == spike`, clamp any planned grid charging to zero regardless of solver output.
5. **Don't trust the tail**: config `forecast_haircut` discounts sell prices beyond ~6 h toward the horizon median (default mild, e.g. 20% of the excess) so distant phantom spikes don't distort near-term decisions. The `estimate` flag and the low/high band are recorded per interval for backtest analysis of forecast quality.

Phase 3's backtester must specifically report **spike capture rate** (revenue earned during actual spike intervals vs the theoretical max if the battery had been full and discharging at max power) — this is the metric that validates the reserve heuristic and its default parameters.

### Planner post-processing
- **Hysteresis**: only switch `action_now` away from the previous action if marginal benefit > `action_switch_threshold_dollars` (default $0.02/horizon) — kills chattering on near-degenerate solutions.
- **Fallback**: solver failure → shift and reuse previous plan (`solver_status: stale`); inputs stale beyond per-input `max_age` (prices 15 min, PV 2 h, SoC 10 min) → publish idle + `status: degraded` (and never write in active mode).

## 4. Outputs

Dry-run sensors via `POST /api/states` (republished every cycle — REST sensors vanish on HA restart):
`sensor.hem_action` (charge/discharge/idle/curtail + reason), `sensor.hem_power_setpoint` (signed kW), `sensor.hem_soc_target`, `sensor.hem_horizon_cost`, `sensor.hem_plan` (full interval list as attribute, apexcharts-card-friendly), `sensor.hem_status` (heartbeat: ok/degraded, last_solve, solve_ms). Document a `recorder: exclude:` snippet for `sensor.hem_plan` (big attribute → DB bloat).

Web UI: FastAPI ingress page (relative URLs only) — one static HTML + vendored apexcharts.min.js, `/api/plan` JSON. Charts: buy/sell price curves, PV + load forecast, planned battery power, SoC trajectory, shaded spike intervals. Also ship a copy-paste `apexcharts-card` Lovelace example in DOCS.md.

## 5. Config (add-on options → pydantic Settings)

```yaml
price_source: amber_express         # amber_express (default) | amber_core
entities: { buy_price, buy_forecast, sell_price, sell_forecast,
            pv_forecast_today, pv_forecast_tomorrow,
            battery_soc, battery_power, weather }
battery:  { capacity_kwh, max_charge_kw, max_discharge_kw,
            efficiency_charge: 0.95, efficiency_discharge: 0.95,
            soc_min: 0.10, soc_max: 1.00,
            wear_cost_per_kwh: 0.04, allow_grid_charge: true }
grid:     { import_limit_kw, export_limit_kw }
load_profile: { weekday_kw: [24 values], weekend_kw: [24 values],
                temp_rules: [{when: temp_above, threshold_c: 28, add_kw: 1.5}, ...] }
optimizer: { horizon_hours: 36, terminal_soc_value: auto,
             solver_timeout_s: 30, action_switch_threshold_dollars: 0.02,
             forecast_haircut: 0.2 }
spike:     { lookahead_hours: 4, reserve_kwh: 6.0,
             high_price_threshold: 1.00, reserve_penalty_per_kwh: 0.50 }
control:   { mode: dry_run, max_writes_per_hour: 12 }   # active = Phase 4
```

## 6. Phased milestones

**Phase 0 — Scaffold + HA connectivity.** Restructure repo (move pyproject/main.py into `hem/`), add repository.yaml/config.yaml/build.yaml/Dockerfile, `config.py`, `ha/client.py`, `ha/publisher.py`, skeleton loop publishing `sensor.hem_status` every 5 min. Pin all deps now (incl. cvxpy/highspy).
*Verify:* (a) `uv run python -m hem` on the Mac → sensor appears in HA Developer Tools; (b) `docker buildx build --platform linux/amd64,linux/aarch64` succeeds (proves the wheel story early); (c) install as local add-on on HAOS (`/addons/`), same sensor via supervisor proxy.

**Phase 1 — Data ingestion + normalization.** Adapters (amber, solar, sungrow, weather), `timegrid.py`, `BaselineLoadForecaster`, `recorder.py` (JSONL to `/data/history/`), `python -m hem.snapshot` CLI printing the aligned grid.
*Verify:* unit tests against fixtures captured from Dan's live entities (commit fixtures); live snapshot cross-checked against HA attributes (units!); grid boundaries land on :00/:30 and step 0 shrinks toward the boundary.

**Phase 2 — Optimizer + dry-run publishing.** `optimizer/model.py`, `planner.py`, full publisher, event-triggered re-solve.
*Verify:* synthetic-scenario unit tests asserting qualitative behavior: (1) evening sell-price spike → pre-charge then full-power export; (2) negative overnight buy price → grid charge; (3) negative midday feed-in → curtailment; (4) flat prices → self-consumption-like; (5) SoC not drained at horizon end. Then run live 48 h and compare `sensor.hem_plan` against the Amber app.

**Phase 3 — Backtesting.** `backtest/sim.py`: receding-horizon replay of recorded JSONL (re-solve each step, apply step-0 decision, roll actuals forward), battery physics + billing model. Baselines: naive self-consumption, no-battery. Report $/day + % uplift.
*Verify:* HEM ≥ self-consumption over ≥ 1 recorded week (if not, fix before ever enabling write mode); **spike capture rate** reported (revenue during actual spike intervals vs theoretical max) to validate the reserve heuristic; energy-conservation unit test on the simulator.

**Phase 4 — Actuation via user-owned automation** (redesigned 2026-07-16; the original in-process `SungrowExecutor` was built, review-hardened, then removed in favor of this — see git history). HEM publishes recommendation sensors only; `blueprints/hem_actuator.yaml` maps `hem_action`/`hem_power_setpoint` onto any inverter via user-supplied action sequences, with the dead-add-on failsafe built in (heartbeat stale/degraded/missing → idle actions). DOCS ships a filled-in mkaiser Sungrow example (power register first, forced mode last).
*Verify:* bench window — inverter follows forced charge → discharge; `docker stop` the add-on and confirm the blueprint reverts to Self-consumption; rate limiter respected.

**Phase 5 — UI + polish.** Ingress charts page; DOCS.md; GitHub Action via `home-assistant/builder` publishing multi-arch images to GHCR + `image:` key (users pull instead of building on-device).
*Verify:* ingress panel renders on desktop + phone app with no external network; fresh HAOS install from the GitHub URL starts in < 1 min.

## 7. Key risks & gotchas

- **cvxpy on Alpine/musl** — the #1 packaging trap; solved by Debian base + Phase 0 multi-arch build check.
- **Amber forecast divergence** — forecasts (spike magnitude/duration especially) routinely miss; mitigated by the layered spike strategy (§ Spike strategy): advanced-price mode, SoC spike reserve, event-triggered re-solve on confirmation, forecast haircut. Backtester tracks spike capture rate to tune the defaults.
- **Unit/sign chaos** — Amber Express negates feed-in prices and reports $/kWh; core reports differently; Sungrow battery-power sign varies; one normalization layer + fixture tests locked to Dan's real entity data.
- **DST/timezones** — NEM is AEST year-round; do everything in UTC (:30 boundaries are DST-invariant), local time only for load-profile hour lookup and UI; include AEDT + transition-day fixtures.
- **Entity unavailability / HA restarts** — per-input max-age policy → degraded mode; REST-published sensors are ephemeral → republish every cycle (MQTT discovery is the clean later upgrade).
- **Write-mode safety** — Forced mode leaves the inverter dumb if HEM dies → HA-side blueprint watchdog is mandatory before enabling active mode; keep register writes minutes-scale.
- **Solver edge cases** — clamp soc0 into bounds pre-model; test the timeout→previous-plan fallback path explicitly.

## 8. Dependencies (hem/pyproject.toml, Python 3.13, uv-locked)

`cvxpy-base >=1.6,<1.7` (no bundled solvers, native HiGHS interface), `highspy >=1.9,<2`, `numpy >=2.1,<3`, `aiohttp >=3.10,<4` (REST + WS, one lib), `pydantic >=2.8,<3` + `pydantic-settings`, `fastapi >=0.115` + `uvicorn >=0.30`. No pandas (timegrid resampling is ~50 lines of numpy). Dev: pytest, pytest-asyncio, freezegun, ruff. Dockerfile installs from the lock (`uv sync --frozen`).
