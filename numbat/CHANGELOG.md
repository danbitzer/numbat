# Changelog

## Unreleased

- **HEM is now Numbat — Energy Optimizer**: the **NUM**erical **BAT**tery
  optimizer (and an endangered striped marsupial from WA). Renamed end to
  end: repository (`github.com/danbitzer/numbat`), add-on slug (`numbat`),
  images (`ghcr.io/danbitzer/numbat-{arch}`), Python package, published
  entities (`sensor.hem_*` → `sensor.numbat_*`,
  `binary_sensor.numbat_vacation_mode`), the actuator blueprint
  (`blueprints/numbat_actuator.yaml`), the config file
  (`/data/numbat-config.json`) and the standalone/dev env vars
  (`HEM_*` → `NUMBAT_*`).
- **BREAKING — new add-on identity; a fresh install is required.** Migration:
  1. On the old HEM (0.10.0+): Settings → Backup → **Export settings**.
  2. Uninstall HEM. The repository entry keeps working (GitHub redirects the
     old URL) and now offers **Numbat — Energy Optimizer** — install it.
  3. Open Numbat → Settings → **Import settings** with the exported file,
     review, and enable.
  4. Re-point the actuator: import `blueprints/numbat_actuator.yaml`, create
     the automation from it (defaults now `sensor.numbat_*`), and delete the
     old HEM automation.
  5. Update any dashboards/recorder rules using `sensor.hem_*`; the old
     entities disappear on the next Home Assistant restart (REST-published
     states are not persisted), and history starts fresh under the new ids.

## 0.10.0

- **Settings backup**: a new **Backup** card at the bottom of Settings.
  **Export settings** downloads the saved config as a JSON file (the exact
  `/data/hem-config.json` document, via `GET /api/config/export`);
  **Import settings** restores one — exported file, copied `hem-config.json`
  or its `.bak` — replacing every setting after a confirmation modal, with
  the same server-side validation as a save. Works on a fresh unconfigured
  install too, so a new box (or a renamed add-on) can be seeded from a
  backup.
- Repository and images moved with the GitHub account rename: the add-on now
  lives at `github.com/danbitzer/hem` and pulls images from
  `ghcr.io/danbitzer/hem-{arch}`. GitHub redirects the old repo URL, but the
  old GHCR namespace does not — this release carries the corrected `image:`
  reference.

## 0.9.0

- **Live/Test mode navigation redesign**: the Dashboard/Settings/Test tabs are
  gone. The header now carries a top-level **Live | Test** mode switch
  (Stripe-style; always lands on Live) and a settings gear that opens settings
  beside the page on wide screens — dashboard and settings visible together at
  last — and as its own page (gear becomes a back arrow) on phones.
- **Test settings sandbox**: in Test mode the gear opens a sandbox copy of the
  live config's battery, grid, optimizer and spike sections. Every simulation
  runs with the sandbox, so any config change can be previewed against
  scenarios or time travel without touching the live settings; **Reset to
  live** re-copies the live config and **Apply to live** promotes the sandbox
  once you're happy. A **Run** button sits beside them, so tweak-and-compare
  loops (e.g. watching the planned SoC chart) never require scrolling back to
  the top of the test column. Replaces the former per-field "Config overrides" list in
  the Test tab — every solver knob is now sandboxable, not just eight of them
  (the simulate API takes whole config sections instead of ad-hoc overrides,
  and the daily-target price multiple now applies in simulations exactly as it
  does live).
- The app bar's plan-meta strip ("computed … · optimal · … ms · … intervals ·
  horizon …") is gone — the bar serves both Live and Test modes, where live
  diagnostics could mislead. The hero card's "Why this action?" panel is now a
  general **"More info"** panel carrying the computed time, solver status and
  solve time alongside the existing explanation; the intervals count is
  dropped and the horizon is already covered by the Horizon cost tile. Plan
  fetch errors now surface in the dashboard column instead of the bar.
- Default battery wear cost lowered from 4c to **3c/kWh**, lining up with the
  realistic lithium range the field help quotes (~0.5–3c). Only affects fresh
  installs — saved configs store the value explicitly, so existing setups keep
  whatever they have.
- Settings polish for the new panel: fields are a single stacked column
  everywhere (label and unit above the control); section cards collapse
  behind their headers (open by default on a fresh install, and any section
  holding a validation error opens itself so a message can never hide);
  the settings panel and the main view scroll independently, keeping the
  Save/Apply bar always in reach — split by a full-height divider, with each
  area's scrollbar at the divider / screen edge instead of overlapping the
  cards; and the dashboard greys out with a "Re-planning…" pill while a save
  waits for the optimizer's re-solve.
- `entities.pv_power` (the actual-PV sensor time travel replays solar from)
  moved out of the live Entities section into Test mode ("Time travel data"
  in the test settings panel) — it only affects simulations. Choosing a
  sensor there saves to the live settings immediately, since simulations
  always read entities from the live config.
- **Import reluctance** (`optimizer.import_penalty_per_kwh`, default 0 = off):
  a virtual per-kWh toll on grid imports in the planning maths (never in
  displayed costs) that biases the plan toward solar and stored energy —
  import-now-to-sell-later bets must beat holding by a bigger margin, since
  the import is certain money and the forecast sell is not. Skipped at
  negative buy prices so paid-to-charge windows stay fully attractive.
  Trialable in the Test-mode sandbox before saving.
- **New guide: [OPTIMIZER.md](OPTIMIZER.md)** — a plain-language explanation
  of how the optimizer decides (what it weighs, the hold value, when it
  sells, the daily target and spike reserve), with worked examples, common
  configuration pitfalls, a setup checklist for new systems, and a
  which-knob-for-which-itch quick reference. Linked from DOCS.
- Hold value scaling help now warns that values above 100% cause
  buy-to-stockpile imports (stored energy valued above its rebuy cost makes
  cheap-window charging look like free money) — seen live at 110%.

## 0.8.0

- **Time travel in Test mode**: pick a past moment and HEM replays the prices,
  solar and house load Home Assistant actually recorded from then through the
  optimizer — see how your current (or overridden) settings would have handled
  a real day instead of a synthetic one. Starts from the battery level recorded
  at that time (or one you set); honest about being hindsight (recorded
  actuals, not the forecast HEM saw); reach limited by the recorder's retention
  (~10 days by default). Real solar needs the new optional `entities.pv_power`
  sensor (your actual PV generation power, e.g. the mkaiser package's
  `total_dc_power`) — without it replays assume zero PV. Read-only, like the
  synthetic scenarios.
- Clarified the "Daily target price multiple" setting (UI help + DOCS): it
  combines with the fixed penalty rather than replacing it — each solve uses
  whichever is higher.
- Settings polish:
  - Ratio settings (SoC min/max, charge/discharge efficiency, daily
    full-charge target, load forecast buffer, hold value scaling, forecast
    haircut) are now displayed and edited as **percentages** in the UI
    (stored values unchanged).
  - **Forecast haircut now defaults to off** and is renamed "Sell price
    forecast haircut": Amber's advanced predicted pricing already tempers
    over-forecast spikes, so a second haircut double-discounted them. Set it
    above 0 only if your price sensor carries raw AEMO-style forecasts.
    (Existing saved configs keep their stored value.)
  - "Solver timeout" removed from the Settings UI (config-file only) — it's a
    never-fires safety valve, and a timeout already falls back to the
    previous plan.
  - Wear cost help now matches the redesigned economics (realistic ~0.5–3c/kWh,
    throughput-only, and it prices house-serving discharge too — use the export
    spread for sell-selectivity) instead of suggesting a 16c-style estimate.
  - Settings help no longer references specific battery brands/integrations —
    sensors are described by function (battery-agnostic).
  - Entity pickers show just the friendly name once selected; entity IDs
    remain visible in the search results.

## 0.7.0

- **Optimizer economics redesign** — fixes the battery selling stored energy
  cheap and not reliably filling to a daily target:
  - **Hold value re-anchored to rebuy cost.** The value of stored energy at the
    horizon end is now the cheapest forward import grossed up for charge losses
    (`min(buy) / efficiency_charge`), scaled by `optimizer.hold_value_scaling`
    and floored above zero by `optimizer.hold_value_floor` (default 1c). The old
    `median × efficiency − wear` formula collapsed to ~$0 on cheap days (so the
    battery would export at any feed-in above the wear cost) and, worse,
    *inverted* the export decision — a higher wear lowered the hold value and
    invited more selling. Wear is no longer subtracted from the hold value, so
    raising it now makes the battery cycle **less**. On a flat/low-spread
    horizon the hold value is capped at the self-consumption break-even so the
    battery still runs the house from stored solar instead of hoarding.
  - **Wear is a throughput cost only** — documented realistic values (~0.5–3c/kWh)
    and that much above ~4c suppresses genuine arbitrage.
  - **Daily target is now a windowed floor.** `battery.daily_target_hold_hours`
    (default 4h) holds the target SoC as a floor from `daily_target_time` through
    the evening peak, instead of a single instant it could dump the moment after.
    The penalty is now per kWh-*hour* of shortfall and can be scaled to dominate
    the tariff via `battery.daily_target_penalty_price_multiple`.
  - **Export floor / deadband.** `grid.min_battery_export_price` sets a hard manual floor
    below which the battery never sells stored energy (PV export and charging
    untouched); `optimizer.min_battery_export_spread` is the automatic counterpart — the
    battery only sells when the feed-in beats the value of holding by a margin,
    killing pennies-margin churn on the 5-minute reprices.
  - The auto hold value is now computed on the real forecast window, not the
    padded tail.
- **Test mode** (a new "Test" tab): run the optimiser against hand-picked
  synthetic Amber price scenarios — "price spike tonight", "negative feed-in
  tomorrow", "low morning rising afternoon", and more — to see how HEM would
  respond without waiting for real prices to change. Pick a scenario and a
  starting battery level, optionally override key settings (wear cost, hold
  value scaling, export deadband, min export price, daily SoC target/penalty)
  to preview a change without saving it, and the resulting plan renders exactly
  like the live dashboard. Read-only — it never touches your live plan or the
  inverter.

## 0.6.0

- **"Why this action?" on the dashboard**: an expandable panel under the
  Action-now hero explains the current interval in plain language and lays
  out the numbers behind it — buy/feed-in prices, solar, house load,
  battery power, SoC start→end, the grid flow and the interval's $ result,
  plus the price's rank in the forecast, the "hold value" it's weighed
  against (with a "?" tooltip explaining what the hold value is), and which
  levers are armed (spike reserve, daily target, live
  spike, estimated price). The reason is a faithful narration of the plan,
  not a guess: the MILP emits a schedule, and the panel reconstructs the
  economics that make that schedule optimal.

## 0.5.2

- Fix the dashboard not scrolling in Home Assistant's iOS companion app
  until you tapped a button. HA renders ingress pages in an iframe inside
  a WKWebView, and WebKit doesn't activate the subframe's touch-scrolling
  until it gains focus; HEM now nudges focus + a 1px scroll on load and on
  first interaction so scrolling works immediately. Safari (which loads the
  page directly, not in a subframe) was never affected.

## 0.5.1

- Dark mode neutrals now track Home Assistant's default dark theme
  (near-black `#111` canvas, `#1c1c1c` cards, `#202020` insets,
  `#e1e1e1`/`#9b9b9b` text, faint divider borders) so the add-on sits
  comfortably beside HA's own dark UI, in place of the previous
  blue-tinted greys. The accent colours and bright chart series are
  unchanged; neutral chart gridlines/ticks track the new palette.
- Settings page is now phone-friendly. The big one: a long selected entity
  label made the whole form (and every card) wider than the screen — the
  form now has a definite width and the entity picker's label truncates
  with an ellipsis. Also: the vacation and theme card actions drop onto
  their own full-width row instead of squeezing the description into a
  sliver, the vacation dialog's end-time row wraps, its footer buttons
  stack full-width, and the header's tab switcher wraps below the title.
  Verified overflow-free down to 320 px wide.
- More mobile polish: on the vacation and theme cards the heading now sits
  on its own row above the description (was cramped beside it), and the
  vacation button is full-width with a comfortable tap target. The header
  meta line wraps instead of truncating to "… interv…", and the
  Dashboard/Settings switcher becomes a full-width segmented control whose
  active tab reads as a raised surface distinct from the bar.
- Dashboard tile "?" help now works on touch screens: devices that can't
  hover get a tap-to-open popover styled like the desktop tooltip.

## 0.5.0

- **Theme setting** (Settings → Theme): choose Light, Dark, or System
  (follow this device's preference — the previous behaviour and still the
  default). Applies instantly and is remembered per browser, like other HA
  add-ons do it — HA ingress gives the add-on no way to read the HA theme.
- Dashboard banners (vacation mode, lifecycle) now update within a couple of
  seconds of saving settings: after a save the plan is re-fetched until the
  post-apply re-solve lands, instead of racing it once and then waiting for
  the next 60 s poll.

## 0.4.0

- **Dashboard redesigned to the Claude Design "HA Cards" direction (1A)**:
  mirrors Home Assistant's native card look — soft grey canvas, white cards
  with soft borders and shadows, HA-blue accent, purple action accent. New
  header bar with a pill Dashboard/Settings tab switcher and a mono meta
  line; an "Action now" hero card with the battery setpoint; a stat row
  (Amber prices coloured like the chart, horizon cost, forecast load);
  restyled
  charts (stepped series in the handoff palette, translucent area fills,
  mono axis labels, legends beside titles) and a bordered planned-mode
  ribbon. Settings gets the same card treatment: 48×28 toggle, single
  vacation pill (state + dialog), inset entity pickers and mono number
  inputs, code chips for entity ids. Dark mode uses the handoff's
  "Nightwatch" neutrals with the 1A accents.
- Dashboard updates now show without a force-refresh: `index.html` is served
  with `Cache-Control: no-cache` (ETag revalidation) so it always points at
  the current hashed bundle; the hashed assets themselves cache as immutable.
- Vacation mode dialog: the end-time picker only appears once "Pick end
  time" is clicked, pre-filled with a concrete suggestion (tomorrow, next
  full hour) — "No end time" is the explicit alternative — and a line states
  exactly what will be saved. Fixes a Safari trap: an untouched
  `datetime-local` displays today's date while its value is still empty, so
  end times were silently saved as "no end".

## 0.3.0

- Dashboard: tile "?" help tooltips are proper styled tooltips (shadcn) with
  keyboard focus support instead of native browser `title` bubbles (#11).

- **Re-solve on every price change**: the $0.05 significance threshold is
  gone — any change of the live buy/sell price (or its estimate flag)
  triggers an early re-solve, so the plan and dashboard reflect the real
  price within seconds of Amber confirming it instead of up to 5 minutes
  later. A 5 s floor between event-driven solves guards against a flapping
  sensor; the 5-minute boundary solve is unchanged. A spike_status flip on
  the spike sensor now also triggers, even before its binary state turns on.
- Dashboard: the Amber buy/sell tile is marked "forecast, unconfirmed" (with
  an explanatory tooltip) while the solve used Amber's estimate for the
  current interval — right at each 5-minute boundary, before the confirmed
  price lands and the re-solve clears it.

- **Vacation mode**: flatten the load forecast to a configured standby
  baseline while the household is away, freeing the whole battery for spikes
  and cheap windows. Enabled from a dialog at the top of Settings
  (baseline kW + optional local end time); auto-expires at the end time, and
  an end inside the horizon reverts later steps to the learned forecast so
  the return evening is already planned. No temperature response and no
  `load.buffer` while active. Surfaced as a dashboard banner and
  `binary_sensor.hem_vacation_mode` (visibility only — the actuator
  deliberately ignores it).

## 0.2.0

- **Configuration moves into the web UI** (#5): a new Settings view (shadcn
  UI + TanStack Form) with per-field inline documentation, searchable entity
  pickers fed by a new `/api/entities` endpoint, server-side validation with
  per-field errors, and save-and-apply without an add-on restart. HEM now
  owns its config at `/data/hem-config.json` (atomic writes, `.bak`,
  `schema_version`); the Supervisor options are reduced to `log_level` only.
  **Breaking**: existing installs must clear the old options from the add-on
  Configuration tab (⋮ → Edit in YAML, leave only `log_level`) and re-enter
  settings in the web UI — there is no migration. A new **HEM enabled**
  master switch (off on first boot / until configured) stops planning cycles
  and publishes `sensor.hem_status` as `disabled`/`unconfigured`, so the
  actuator blueprint's failsafe keeps the inverter in self-consumption;
  `/health` stays healthy in those states so the watchdog doesn't
  restart-loop a deliberately disabled add-on. Standalone dev uses
  `./hem-config.json` (via the same UI); `dev-options.json` and
  `HEM_OPTIONS_FILE` are gone.
- **`battery.daily_target_hour` is now `battery.daily_target_time`** (HH:MM,
  default 15:00): the daily full-charge target supports minutes and is a
  proper time picker in the Settings view.
- **`load.buffer`** (default 0): safety margin on the learned load forecast —
  the whole forecast (temperature response included) is scaled by
  `1 + buffer`, so 0.1 plans for 10% more house load everywhere. Shown on the
  dashboard's load-forecast line when active.

- **Dashboard rewritten in React** (#3): React 19 with the React Compiler,
  TypeScript, Recharts, Tailwind — built by Vite/Bun into the same fully
  offline ingress bundle. Feature parity with the old page (tiles, meta and
  load-forecast lines, warning banner, padded-tail band, all charts), plus
  the mode strip now joins the synced hover crosshair. The `/api/plan`
  contract is unchanged — now expressed as Zod schemas that validate every
  response, with polling handled by TanStack Query.

## 0.1.9

- **Soft daily SoC target** (`battery.daily_target_soc`, off by default):
  softly requires the battery at a target SoC by a local hour each day
  (default 3pm), paying at most `daily_target_penalty_per_kwh` ($0.10) per
  missing kWh. Prices the insurance value of a full battery against
  unforecast spikes and surprise load, which the pure forecast economics
  assign zero worth — on mild days the optimizer would otherwise stop at
  "enough for the forecast". Binds at an instant, not a floor: the battery
  still discharges freely into the evening peak.

## 0.1.8

- **Below-reserve SoC is no longer clamped up to `soc_min`**: the plan starts
  from the actual SoC (phantom energy was invented when a BMS recalibration
  or overnight self-consumption load left the battery under the reserve),
  never discharges below the real level, and recovers above the reserve when
  prices favor charging. DOCS now spells out `soc_min` as HEM's planning
  reserve vs the inverter's own minimum.

## 0.1.7

- Dashboard: "Amber buy / sell" tile — the live prices the current action was
  optimized against, with the 5-minute interval they apply to (#1).
- Dashboard: hover tooltip on the Horizon cost tile explaining what the
  number is (net meter cash flow over the horizon; excludes wear and
  terminal stored value); DOCS sensor table clarified to match.

## 0.1.6

- Dashboard: a load-forecast info line under the header — how many days of
  history the daily learn used, from which sensor and source (long-term
  statistics vs recorder history), hour-bucket coverage, and the fitted
  temperature response (sensor + peak kW/°C heating/cooling).
- **`load_forecast.history_days` option removed**: learning now always reads
  up to 365 days and self-caps to the history that actually exists — more
  data is strictly better, so there was nothing to configure. If the add-on
  complains about an unknown option after updating, remove the
  `load_forecast:` section from its Configuration (⋮ → Edit in YAML).
- **Backtesting removed** (`hem.backtest`, the `/data/history` JSONL recorder,
  and the `HEM_DATA_DIR` env var): the project is validated by reviewing the
  dry-run dashboard and monitoring live behaviour instead of programmatic
  replay. The add-on no longer writes anything to `/data` except its options.

## 0.1.5

- **`sensor.hem_plan` removed**: nothing consumed it (the dashboard reads the
  plan from the add-on directly) and its large attribute churned the recorder
  every 5 minutes. If you added a `recorder: exclude:` for it, you can drop
  that; the entity disappears on your next HA restart.
- Dashboard: the mode strip, SoC chart, and line charts now share one y-axis
  gutter and the exact plan time-span, so all charts align column-for-column.
  The SoC right-hand % axis is gone (it forced the plot out of alignment) —
  the tooltip shows kWh and % instead. Mode-strip tooltip follows the cursor.

## 0.1.4

- **`hold` replaced by `no_charge`**: the earlier `hold` action froze the
  battery (forced mode + stop), which wrongly imports instead of covering a
  load dip while deferring a charge. `no_charge` is self-consumption with
  charging blocked (Sungrow: max charge power 0), so the battery still serves
  the house. Blueprint gains `no_charge_actions` and a `restore_actions`
  sequence (max charge power back to full, run before every branch so the
  cap can't stick). The reverse case (block discharge to hold the reserve) is
  deferred to a future `no_discharge` action.
- Dashboard: setpoint tile shows "—" for every non-forced mode; the mode
  timeline gains a `no_charge` colour.


## 0.1.3

- Dashboard: new "Planned mode" timeline strip — the horizon colored by
  action (charge/discharge/hold/curtail/idle) at a glance.
- Dashboard: the setpoint tile shows "—" during idle/curtail (the battery is
  under self-consumption control; there is no commanded setpoint).
- Blueprint: the grid-connection input is a single binary sensor now
  (was a list) — re-select your sensor after re-importing.

## 0.1.2

- **New `hold` action**: the battery stays deliberately inactive while PV
  surplus exports (deferring the charge to a lower-value window) or load
  imports (saving stored energy for a better price) — jobs self-consumption
  mode cannot do. Blueprint gains an optional `hold_actions` input (Sungrow:
  forced mode + Stop); left empty, hold behaves as idle.
- Blueprint: optional grid-connection sensor(s) — any reading OFF reverts to
  idle/self-consumption immediately and re-asserts every 5 minutes.
- Price-event debounce reduced 10s -> 2s: HEM re-solves ~3s after a
  significant Amber price lands.

## 0.1.1

- **Grid-coupled action semantics**: `charge`/`discharge` are now reserved for
  moves your inverter's self-consumption mode would never make on its own —
  `charge` means charging from the grid, `discharge` means exporting stored
  energy. Running the house off the battery and charging from PV surplus both
  publish `idle`, so the actuator leaves the inverter in load-following
  self-consumption instead of pinning a forced setpoint.
- Blueprint: optional `curtail_actions`/`uncurtail_actions` inputs for
  negative feed-in export capping, with the un-cap wired into every branch
  including the failsafe.
- Publisher: `sensor.hem_action` carries `power_kw`/`power_w` attributes
  (atomic with the action); the blueprint reads power from there.
- Solver-failure fallback (reuse the previous plan shifted forward) now
  actually runs in production.
- Load learner: per-day bidirectional unit-mislabel correction, local-hour
  splitting of statistics rows (removes a ~30-min profile lag), bounded daily
  learn with proper retry backoff.
- First price/spike change after a restart triggers an early re-solve.

## 0.1.0

- Initial release: rolling-horizon MILP battery optimizer for Amber Electric
  5-minute pricing, learned load forecasting with temperature response,
  spike-reserve hedging, dry-run recommendation sensors, ingress dashboard,
  actuator blueprint with heartbeat failsafe, receding-horizon backtester.
