# Home Energy Manager (Numbat)

Numbat reads Amber Electric prices, an Open-Meteo solar forecast, your battery state, and a
temperature forecast from existing Home Assistant integrations, then solves a
mixed-integer optimization every 5 minutes to plan battery charge/discharge and solar
export over the next ~36 hours.

In **dry-run mode** (the default) Numbat only publishes its recommendations as sensors —
nothing is written to your inverter.

## Prerequisites

- [Amber Express](https://github.com/hass-energy/amber-express) with its pricing mode
  set to **advanced price** (the core Amber Electric integration is not supported —
  its forecast attribute has 1c price resolution and no advanced-price mode)
- [Open-Meteo Solar Forecast](https://github.com/rany2/ha-open-meteo-solar-forecast)
- Battery SoC/power sensors (e.g. the
  [mkaiser Sungrow Modbus package](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant))
- Optionally, any `weather.*` entity with hourly forecasts (enables the
  learned temperature response)

## Configuration

**Numbat is configured in its own web UI, not on the add-on's Configuration
tab.** Open the Energy Manager panel and go to **Settings**: every field has
inline documentation, entity fields are searchable pickers with friendly
names, and saving validates the whole document server-side and applies it
before the next cycle — no add-on restart. The add-on's Supervisor options
hold only `log_level`.

**A fresh install starts disabled**: no planning cycles run and
`sensor.numbat_status` reports `unconfigured`, which keeps the actuator
blueprint's failsafe in self-consumption. Configure your entities and flip
the **Numbat enabled** switch to start planning. The same switch is the master
off-switch later: turning it off publishes `sensor.numbat_status` as `disabled`
(anything other than `ok` makes the actuator revert the inverter to
self-consumption on its next 5-minute sweep).

The config lives in `/data/numbat-config.json` (kept across restarts and
updates, previous version in `.bak`). Note ingress is HA-session
authenticated: any logged-in HA user who can open the panel can edit the
config — the same trust level as the rest of the dashboard.

**Backup** (Settings → Backup, at the bottom): **Export settings** downloads
the saved config as a JSON file — the exact `numbat-config.json` document — and
**Import settings** restores one, replacing every setting after a
confirmation. Import accepts an exported file, a copied `numbat-config.json`, or
its `.bak`, and validates it exactly like a save. Use it before experiments,
or to seed a fresh install.

The sections below document what each setting means in depth. Values quoted
here are what the config **document** stores; ratio-type settings (SoC
bounds, the daily target, the load buffer, hold value scaling, the daily
target price multiple, the forecast haircut) are stored as fractions but
displayed and edited as **percentages** in the Settings UI.

### `entities`

Point each option at your entity IDs. Amber Express's forecast attributes live on
the price sensors themselves, so the two price entities are all Numbat needs for
both live prices and forecasts.

`weather` (optional) is any weather entity with hourly forecasts. It supplies
the forecast temperatures the learned temperature response is applied to;
without it, load planning is time-of-day only (`load_power` history and
`outdoor_temp` learning still work — there's just no forecast to project the
response onto).

`pv_power` (optional) is your **actual** PV generation power sensor (W or kW,
e.g. your inverter's total DC power) — distinct from the `pv_forecast_*`
forecast sensors. It feeds Test mode's time travel, which replays real
recorded solar; without it, historical replays assume zero PV. Because it only
matters to Test mode, the UI shows it there ("Time travel data" in the test
settings panel) rather than in the live Entities section.

### `battery`

Physical parameters of your battery. `wear_cost_per_kwh` is the degradation cost charged
against every discharged kWh in the objective. It is a *throughput* cost only — it never
lowers the value of stored energy (see the hold value under `optimizer`), so raising it
makes the battery cycle **less**, as you'd expect. A reasonable value is the *wearing
part's* replacement cost ÷ its lifetime throughput — and where you draw that line is a
judgment call, so know the bounds. Strictly the cells: a future cell-stack replacement
at ever-falling cell prices (e.g. 32 kWh at ~$150/kWh ≈ $5,000, over 8,000 cycles ≈
256,000 kWh → ~2c/kWh). Broadly the full installed price ÷ the same throughput →
6–10c — but that charges cycling for the inverter, wiring and labour (which wear far
slower, on their own clock) and ignores that at ~1 cycle/day the cells usually reach
calendar end-of-life before they run out of cycles. Reality sits between — a cell swap
needs labour too — and defensible answers land at **~0.5–3c/kWh**. The default is 3c,
the cautious top of that range.

Note that "every discharged kWh" **includes serving your own house**: the battery only
covers the load when the buy price beats roughly `wear + hold value/efficiency`, so an
inflated wear cost makes it sit idle holding stored solar while you import at prices it
should be beating. If what you actually want is "don't *sell* for thin margins", keep the
wear cost honest and express that preference with `optimizer.min_battery_export_spread` —
it gates only battery→grid export (the spread is your minimum net profit per exported
kWh, after wear), leaving self-consumption and spike response untouched.

**Daily full-charge insurance** (`daily_target_soc`, default 0 = off): a
rational optimizer only charges enough for the *forecast* — unforecast spikes
and surprise usage are worth nothing to it, so on a mild day it may stop at
50%. Setting `daily_target_soc` (e.g. `1.0`) softly requires that SoC from
`daily_target_time` local time (default 15:00, before the evening ramp) and
**held for `daily_target_hold_hours`** (default 4h) — a floor across the
evening peak, not a single instant it can dump the moment after. Freed to
discharge once the window ends. Soft means: the plan pays up to
`daily_target_penalty_per_kwh` (default $0.05) per kWh-*hour* of shortfall —
your insurance premium. Filling via forgone feed-in or a cheap grid window
happens; sacrificing a genuinely better opportunity, like exporting into a
real spike, does not.

**Calibrate the penalty against your tariffs**: it is a maximum
willingness-to-pay, so anything cheaper than it WILL be bought. Remember it
accrues per hour of the hold window — the default $0.05 over a 4 h hold
makes a kWh missing for the whole window worth up to $0.20 to avoid, above
most evening import premiums. If the battery still won't reach
the target on dear evenings, either raise it or set
`daily_target_penalty_price_multiple` (0 = off; edited as a **percentage**
in the UI, stored as a fraction like the other ratio settings). The multiple
works **with**
the fixed penalty, not instead of it: each solve uses whichever is higher —
the fixed penalty, or the multiple × the median forward import price — so the
penalty tracks the tariff and the target dominates dear days too. Because the
penalty accrues per hour of the hold window, modest values dominate: 100% of
the median over a 4 h hold already makes a full-window missing kWh worth ~4×
the going rate. 50–150% is plenty; larger values only raise what an
extreme-priced day may cost you.
Note that a single-instant fill cannot survive a *negative* feed-in 
tomorrow (refilling is then free, so the battery may dump tonight and still 
hit the target) — that is what the export floor / deadband below is for.

`soc_min` is the hard floor for **all planned discharge — including serving
your own house**. Once a plan grinds down to it, remaining load is met by
grid imports: every point above the inverter's own minimum is energy the
plan *defends with imports* rather than spends. That makes it poor insurance
against forecast error — for hungry-day insurance use `load.buffer`, which
holds extra energy the plan can still spend when the day needs it. Keep
`soc_min` at, or just above, the inverter's enforced minimum; anything
higher should be energy you deliberately never want planned away (e.g. a
blackout reserve).

Two mechanics worth knowing. The floor binds the *plan*, not the inverter:
during `idle`, self-consumption serves house load from the battery below the
floor regardless (down to the inverter's own minimum) — so a floor set well
above it also makes plans budget imports that reality never draws, quietly
distorting the economics upstream. And if the battery is *found* below
`soc_min` (overnight load, a BMS recalibration), the floor relaxes to the
real SoC: the plan never discharges it further and charges back above the
reserve only when prices make that worthwhile.

`export_reserve_soc` (default 10%; a percentage in the UI; 0 = off) is the
sell
floor that `soc_min` is not: battery→grid export may never take — or leave —
the battery below it, while serving your own house still may, down to
`soc_min`. It binds only while `soc_min` sits below it; with the default
`soc_min` of 5%, the 10% reserve is active out of the box. "Use the top 75% for trading, keep the bottom 25% for the house."
It is one-way: it blocks sales, never forces charging back above itself, so
it never defends itself with imports; below it the battery behaves exactly
like the price-floored mode — covering the house's uncovered load, nothing
routed to the grid (not even by displacing PV). Pair it with `soc_min` at
the inverter's enforced minimum and `load.buffer` for hungry-day insurance —
three knobs, each doing one job.

### `grid`

Limits of your **grid connection**, distinct from the battery's power limits:

- `import_limit_kw` — maximum net draw from the grid for the whole house
  (connection/main-breaker capacity).
- `export_limit_kw` — maximum net feed-in allowed by your DNSP/connection
  agreement. This caps what actually reaches the grid regardless of how hard
  the battery discharges: export = battery discharge + PV − house load. If
  you raise `spike.discharge_kw`, raise this to match (if permitted) or the
  extra battery power has nowhere to go.
- `min_battery_export_price` (optional, blank by default) — a **hard** floor: the
  lowest feed-in price ($/kWh) at which Numbat will discharge the *battery* to the
  grid. Below it the battery still runs the house but won't sell stored energy;
  surplus PV can still export, and charging is untouched. Use it if you'd
  rather keep charge than sell it cheap. The automatic
  `optimizer.min_battery_export_spread` deadband does the same thing relative to the
  hold value; this is the fixed-dollar manual override.

`battery.max_charge_kw` / `max_discharge_kw` limit the *battery* (cell wear,
inverter DC side); the grid limits cap the *net AC flow at the meter*. The
optimizer respects both simultaneously.

### Load forecasting

The household load forecast is **learned from your actual consumption** —
there is no hand-typed profile and nothing to configure beyond the entities.
Once a day Numbat reads hourly long-term statistics of `entities.load_power` (a
house load sensor in W or kW — e.g. the mkaiser package's `sensor.load_power`)
over the last **365 days** — the window self-caps to however much history the
sensor actually has — and builds hour-of-day averages, split weekday/weekend,
in your local timezone. Long-term statistics survive recorder purging, so the
window genuinely grows toward a full year; if the sensor has no `state_class`
(hence no statistics), Numbat falls back to raw recorder history (limited to
your purge window, ~10 days). Hours with under 2 observed hours of data use
the mean of the hours that do have data. The dashboard shows what each learn
used: window length, data source, and the fitted temperature response.

Add `entities.outdoor_temp` (any outdoor temperature sensor with long-term
statistics) and the daily learn also fits a **temperature response**: how many
kW your house adds per degree above 22°C (cooling) and below 15°C (heating),
regressed from the same window. Forecasts then apply the *forecast*
temperature to those slopes — so a heatwave arriving after a mild fortnight
raises the load forecast immediately, instead of the trailing average lagging
the weather. The model is only fitted on hours where load and temperature
history overlap, so a newly added temperature sensor shortens the effective
learning window rather than skewing the fit (it grows back as statistics
accumulate).

**`load.buffer`** (default 0) adds a safety margin on top: the whole forecast
is scaled by `1 + buffer`, so `0.1` plans for 10% more house load everywhere
— including temperature-driven peaks. The learned profile is a *mean*; buffer
it if you'd rather the planner run conservative (slightly more kept in the
battery, exports rated slightly less affordable). Pick the right insurance
knob: the buffer shapes the **forecast**, while `battery.soc_min` (reserve
floor) and the daily full-charge target shape **SoC policy** — stacking all
three overlaps. The dashboard's load-forecast line shows the active buffer.

**Vacation mode** (Settings → "Enable vacation mode…"): while the household
is away the learned profile books phantom evening load, so the planner holds
back energy for cooking and AC that won't happen — and under-commits to
spikes. Vacation mode replaces the forecast with a flat standby baseline
(`baseline_kw` — fridge, network, pumps; typically 0.2–0.4 kW, check your
load sensor overnight), with no temperature response and no `load.buffer`
applied. An optional end time (local) auto-expires it; if the end lands
inside the planning horizon, steps after it already use the learned forecast
— the plan covers your return evening before you're home. While active the
dashboard shows a banner and `binary_sensor.numbat_vacation_mode` is `on`
(visibility only — the actuator does not read it; Numbat's own plans already
reflect the baseline).

**Without `entities.load_power`** (or before learning first succeeds) Numbat
plans with **zero house load** and flags it: `sensor.numbat_status` carries
`load_forecast: unconfigured|pending` and the dashboard shows a warning.
Plans still work, but they'll overestimate what's exportable and may run the
battery lower than you'd like — **raise `battery.soc_min`** to keep a comfort
buffer until learning is active. The goal state is always a learned forecast.

### `optimizer`

> **New to how the optimizer thinks?** [OPTIMIZER.md](OPTIMIZER.md) explains
> the whole decision model in plain language — what it weighs, the hold
> value, when it sells, worked examples, and the common configuration
> pitfalls. Recommended before tuning anything below.

- `horizon_hours` (36) — how far ahead each plan looks. Longer sees more of
  tomorrow's solar; beyond the price forecast the tail is padded (shaded on
  the dashboard).
- `terminal_soc_value` — the **hold value**: what leftover stored energy is
  worth at the horizon end, in **$/kWh** (it is NOT a target SoC). `auto`
  anchors it to **rebuy cost** — the cheapest forward import price grossed up
  for charge losses (`min(buy) / efficiency_charge`), i.e. what it would cost to
  put that energy back — then applies `hold_value_scaling` and the
  `hold_value_floor`. On a flat/low-spread horizon it is capped at the
  self-consumption break-even so the battery still runs the house from stored
  solar rather than hoarding. Crucially it does **not** subtract wear, so wear
  no longer inverts the export decision. Without a hold value the optimizer
  would dump the battery at any positive price before the horizon. Enter `auto`
  or a fixed number.
- `hold_value_floor` (0.01) — lower bound on the auto hold value, so a cheap day
  never values stored energy at ~$0 (which was what made the battery export at
  low feed-in prices). Predbat uses ~1c.
- `hold_value_scaling` (1.0) — multiplier on the auto hold value. `>1` makes the
  battery holdier (keeps charge longer), `<1` trades more freely.
- `min_battery_export_spread` (default 0.05; 0 = off) — automatic export
  **deadband**: the battery only sells to the grid when the feed-in beats the
  value of holding (`hold_value / efficiency_discharge + wear`) by at least
  this margin, so it won't churn export for pennies on the 5-minute reprices.
  The automatic counterpart to `grid.min_battery_export_price`. The 5c
  default keeps a fresh install from cycling the battery for sub-wear-cost
  margins; set 0 to sell whenever marginally profitable.
- `import_penalty_per_kwh` (default 0.05; 0 = off; "Import reluctance" in the
  UI) — a
  **virtual** toll added to every imported kWh in the objective, never in the
  displayed costs. A risk-preference knob, not economics: the optimizer is
  risk-neutral and happily imports now against a forecast sell later; the
  toll makes import-dependent bets need bigger margins, biasing the plan
  toward solar and stored energy. Genuine spikes clear any sensible toll.
  Skipped per-interval when the buy price is negative, so paid-to-charge
  windows keep their full appeal. Side effect to know: grid top-ups toward
  the daily target also pay the toll, so an aggressive value may need a
  higher target penalty. A few cents is a sensible starting point.
- `solver_timeout_s` (30, max 60) — HiGHS time limit per solve. Config-file
  only (not in the Settings UI): normal solves take tens of milliseconds, so
  this is a never-fires safety valve — and if it ever does fire, the planner
  falls back to shifting the previous plan forward (`solver_status: stale`)
  rather than doing anything abrupt.
- `action_switch_threshold_dollars` (0.02) — hysteresis: the current action
  only changes if switching improves the horizon objective by more than this.
- `forecast_haircut` (0 = off; "Sell price forecast haircut" in the UI) — the
  share of the above-median excess shaved off sell prices more than 6 h out,
  so distant phantom spikes don't distort near-term decisions. Off by
  default: Amber's advanced predicted pricing (the recommended sensor mode)
  already tempers over-forecast spikes, so a second haircut double-discounts
  them — turn it up only if your price sensor carries raw AEMO-style
  forecasts. The spike reserve reads raw prices, unaffected.

### `spike`

When Amber flags a potential price spike within `lookahead_hours`, Numbat softly reserves
`reserve_kwh` in the battery so it can sell into the spike if it confirms.

`discharge_kw` optionally raises the discharge cap **only while the spike binary
sensor confirms a spike, and only for the current interval** — everyday operation
keeps the wear-conscious `battery.max_discharge_kw`. Spikes are rare enough that
a few full-power hours add negligible cell wear while capturing peak revenue.
Set it to your inverter's true limit (0 disables). Note the extra power only
reaches the grid if `grid.export_limit_kw` allows it.

## Published sensors

| Entity | Meaning |
|---|---|
| `sensor.numbat_status` | `ok` / `error` / `disabled` / `unconfigured`; heartbeat with solve stats and `load_forecast`. Anything other than `ok` makes the actuator blueprint fail safe to self-consumption |
| `sensor.numbat_action` | recommended action now: charge / discharge / idle / no_charge / curtail (carries `power_kw`/`power_w` attributes, atomic with the action) |

Actions are **grid-coupled**: `charge` means charging *from the grid*, and
`discharge` means exporting stored energy *to the grid* — the moves your
inverter's self-consumption mode would never make on its own. Running the
house off the battery and soaking up PV surplus both publish `idle`, because
self-consumption mode already does those jobs with second-by-second load
tracking that a 5-minute forced setpoint can't match.

`no_charge` is self-consumption with **charging blocked**: the plan defers
storing PV to a cheaper window (morning surplus exported at a good price now,
the free midday PV stored later) while the battery still covers a load dip.
Sungrow: leave self-consumption mode on and set the **battery max charge
power to 0** (freezing the battery outright with forced mode + Stop is wrong —
it would import instead of covering the dip). Because that limit is sticky,
also configure a **restore** sequence that puts max charge power back to full;
it runs before every other branch so a later charge or idle isn't left capped.
Without a no_charge sequence the blueprint falls back to idle and the deferral
is lost.

> The mirror case — block *discharging* to hold the spike reserve while the
> grid serves the load — currently actuates as plain idle (the battery may
> discharge to load). A future `no_discharge` action will handle it distinctly.
| `sensor.numbat_power_setpoint` | recommended battery power, kW (+charge / −discharge) |
| `sensor.numbat_soc_target` | planned SoC at end of the current interval |
| `sensor.numbat_horizon_cost` | expected net meter cash flow ($) over the horizon: imports at forecast buy prices − exports at forecast sell prices; negative = earning. Excludes wear cost and the value of energy still stored at the horizon end |

These sensors are republished every cycle and disappear on HA restart until the next
cycle (~5 min). The full interval-by-interval plan is not published as a sensor —
it's a lot of data for the recorder to store every 5 minutes; view it on the
dashboard instead.

## Dashboard

The add-on serves an ingress panel (sidebar → Energy Manager): current
action/setpoint/SoC/cost tiles plus charts of prices, PV/load forecast, planned
battery power with grid flows, and the SoC trajectory. Auto-refreshes every
minute; fully offline (no CDN).

The page has two top-level modes, switched in the header: **Live** (the real
plan, and settings that apply immediately when saved) and **Test** (a
simulation sandbox — nothing in it touches your live plan or the inverter).
The gear button opens settings beside the page on wide screens and as its own
page on phones; what it opens depends on the mode.

### Test mode

Test mode runs the optimizer against data of your choosing, in two ways:

- **Scenarios** — hand-made price shapes ("price spike tonight", "negative
  feed-in tomorrow", …) with a starting-SoC slider.
- **Time travel** — pick a past moment and Numbat replays the prices, solar and
  load Home Assistant actually recorded from then (starting from the battery
  level recorded at that time, or one you set). Honesty caveat: the replay
  feeds the optimizer *recorded actuals* — perfect hindsight — not the
  forecast Numbat saw at the time, so it shows how your settings value the real
  day, not a re-run of the historical decision. Reach is limited by HA's
  recorder retention (~10 days by default), and real solar needs the optional
  `entities.pv_power` sensor — set under "Time travel data" in the test
  settings panel (it saves to your live settings; simulations always read
  entities from the live config).

In Test mode the gear opens **test settings**: a sandbox copy of the live
config's battery, grid, optimizer and spike sections. Every simulation uses
the sandbox, so you can preview any config change — a different wear cost, a
bigger battery, a new export floor — without touching your live settings.
**Reset to live** re-copies the live config; **Apply to live** promotes the
sandbox sections to the real config once you're happy with them.

## Controlling your inverter (the actuator automation)

Numbat never writes to your inverter. It publishes recommendations; a Home
Assistant automation that **you** own turns them into control — inspectable,
traceable, editable, and disabling it is the master off-switch.

Import `blueprints/numbat_actuator.yaml` from this repo (Settings → Automations →
Blueprints → Import), then create an automation from it. You supply three
action sequences for your hardware — plus two optional ones for curtailment —
and inside them the variables `power_kw` (signed, +charge/−discharge),
`power_w` (magnitude in watts), and `action` are available. The blueprint has
the failsafe built in: if Numbat's heartbeat is stale, its status reports an
error, or the sensors
are missing (HA restarted while Numbat was down), your *idle* actions run (after
lifting any export cap) — so a dead add-on can never leave the inverter stuck
in forced mode or curtailed. Keep the idle sequence simple and idempotent.

Optionally point the blueprint at a **grid-connection binary sensor** (ON
while the grid is up — most inverter integrations expose one). During a grid
outage the automation immediately reverts to idle/self-consumption and
re-asserts it every 5 minutes: forced charge or discharge is meaningless
(and battery-hostile) while islanded. A sensor reading `unavailable` is
treated as connected so a flaky sensor can't idle your plan.

Example sequences for the mkaiser Sungrow package (verify entity IDs and
option strings against your install — they vary between package versions):

```yaml
# charge_actions
- action: number.set_value
  target: {entity_id: number.sungrow_battery_forced_charge_discharge_power}
  data: {value: "{{ power_w }}"}
- action: select.select_option
  target: {entity_id: select.sungrow_battery_forced_charge_discharge_cmd}
  data: {option: "Forced charge"}
- action: select.select_option
  target: {entity_id: select.sungrow_ems_mode}
  data: {option: "Forced mode"}

# discharge_actions — as above with option: "Forced discharge"

# idle_actions (also the failsafe — keep robust)
- action: select.select_option
  target: {entity_id: select.sungrow_battery_forced_charge_discharge_cmd}
  data: {option: "Stop (default)"}
- action: select.select_option
  target: {entity_id: select.sungrow_ems_mode}
  data: {option: "Self-consumption mode (default)"}

# no_charge_actions (optional) — self-consumption with charging blocked; the
# blueprint runs idle_actions first, so EMS mode is already self-consumption
- action: number.set_value
  target: {entity_id: number.sungrow_battery_max_charge_power}
  data: {value: 0}

# restore_actions (optional, required with no_charge) — max charge power back
# to full (your battery's rating); runs before every branch so no_charge's 0
# can't cap a later charge or idle
- action: number.set_value
  target: {entity_id: number.sungrow_battery_max_charge_power}
  data: {value: 12000}

# curtail_actions (optional) — cap export while feed-in is negative; the
# blueprint runs idle_actions first, so the battery is already back to normal
- action: number.set_value
  target: {entity_id: number.sungrow_export_power_limit}
  data: {value: 0}

# uncurtail_actions (required if you set curtail_actions) — restore your
# normal export limit; runs before every non-curtail branch, so it must be
# idempotent. Use your DNSP limit in watts.
- action: number.set_value
  target: {entity_id: number.sungrow_export_power_limit}
  data: {value: 12000}
```

Set the power register **before** engaging forced mode (as above), so a
partial failure leaves the inverter in its previous mode rather than forced
with a stale setpoint. Note some mkaiser versions gate the export limit
behind `switch.sungrow_export_power_limit_mode` — if yours does, enable it in
curtail and disable it in uncurtail instead of writing your DNSP limit back.

**Do not create the automation until you've watched Numbat's dry-run
recommendations for at least a few days** and they consistently make sense
against your prices and household load. Then bench-test it: watch the
inverter follow a charge → discharge → idle transition, then stop the Numbat
add-on and confirm the failsafe reverts to self-consumption within your
configured heartbeat age — and keep an eye on its decisions for the first
few weeks.
