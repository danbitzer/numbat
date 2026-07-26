# Setting up Numbat from a fresh Home Assistant install

This walks from a clean Home Assistant OS install to Numbat planning (dry-run),
and then — only after you've reviewed its plans for a while — to actual
inverter control. Every
step before "Install the Numbat add-on" is an ordinary HA integration that Numbat
merely reads, so if you already have some of them, skip ahead.

What you'll end up with:

| Piece | Provides | Source |
|---|---|---|
| Amber Express | Buy/sell prices + Amber's advanced forecast + spike flag | HACS |
| Open-Meteo Solar Forecast | PV production forecast | HACS |
| BOM weather (or any `weather.*` + outdoor temp sensor) | Hourly temperature forecast + observed outdoor temperature | HACS |
| Battery integration | SoC %, battery power, house load (load learning) | e.g. mkaiser Sungrow |
| **Numbat add-on** | The optimizer + recommendation sensors + dashboard | this repo |
| Actuator automation | Turns recommendations into inverter control | blueprint, later |

## 1. Prerequisites

- Home Assistant OS or Supervised (the add-on needs the Supervisor). Amber
  Electric as your retailer, on wholesale pricing.
- [HACS](https://hacs.xyz/docs/use/download/download/) installed — both price
  and solar-forecast integrations come from it.
- An Amber API token: create one at
  [app.amber.com.au/developers](https://app.amber.com.au/developers).

## 2. Amber Express (prices)

Numbat supports [Amber Express](https://github.com/hass-energy/amber-express)
only — the core `amberelectric` integration's forecasts have 1c resolution and
no advanced-price mode, which is not good enough to optimize against.

1. HACS → search for **Amber Express** (add
   `https://github.com/hass-energy/amber-express` as a custom repository if it
   isn't listed) → download, restart HA.
2. Settings → Devices & services → Add integration → Amber Express → paste your
   API token and pick your site.
3. **Set the pricing mode to "advanced price"** in the integration options.
   This makes the `forecast` attribute carry Amber's own SmartShift price
   prediction instead of raw AEMO forecasts, which over-predict spike duration
   by hours. Numbat assumes this mode.
4. Note the entities it created — you'll need:
   - `sensor.amber_express_<site>_general_price` (buy)
   - `sensor.amber_express_<site>_feed_in_price` (sell)
   - `binary_sensor.amber_express_<site>_price_spike`

## 3. Open-Meteo Solar Forecast (PV)

1. HACS → **Open-Meteo Solar Forecast**
   ([rany2/ha-open-meteo-solar-forecast](https://github.com/rany2/ha-open-meteo-solar-forecast))
   → download, restart.
2. Add the integration with your latitude/longitude, panel declination (tilt),
   azimuth, and total DC kWp. If your array has multiple orientations, prefer
   one config entry that models the whole array (or sum per-plane sensors into
   template sensors) — Numbat reads a single pair of entities.
3. Note `sensor.<name>_energy_production_today` and `..._tomorrow`. Numbat uses
   their `watts` attribute (15-min resolution), not the state value.

## 4. Weather — BOM (forecast + outdoor temperature)

Numbat wants two temperature entities:

- a **`weather.*` entity** with hourly forecasts (`entities.weather`) — the
  *forecast* temperatures that the learned temperature response is applied to;
- an **outdoor temperature sensor** (`entities.outdoor_temp`) — the *observed*
  temperatures the response is learned from.

For Australia (this is an Amber-focused product, after all), the
[Bureau of Meteorology integration](https://github.com/bremor/bureau_of_meteorology)
provides both in one install:

1. HACS → **Bureau of Meteorology** → download, restart.
2. Add the integration for your location, with the weather entity and
   observation sensors enabled.
3. Note the two entity IDs: `weather.<location>` (hourly forecasts) and the
   observation temperature sensor `sensor.<location>_temp` (records long-term
   statistics, which the learning reads).

Any other combination works too — e.g. the built-in Met.no entity for the
forecast plus a physical outdoor sensor — as long as the weather entity
answers `weather.get_forecasts` hourly and the temperature sensor has
`state_class: measurement`. If either is missing, Numbat still plans; it just
loses the temperature response.

## 5. Battery and inverter sensors

Numbat needs two sensors from whatever integrates your battery:

- **SoC** in % (or 0–1)
- **battery power** in W or kW (units must be on the entity)

For Sungrow hybrids the established path is the
[mkaiser Sungrow Modbus package](https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant):
enable Modbus TCP on the inverter (or via the WiNet dongle), copy the package
YAML into your config, and you get `sensor.battery_level`,
`sensor.battery_power`, plus the control entities the actuator automation will
use later.

Two things to check:

- **Sign convention**: watch `sensor.battery_power` while the battery charges.
  If it reads *negative* while charging (mkaiser default), Numbat's default
  `power_convention: charge_negative` is correct; if positive, set
  `charge_positive`.
- **House load sensor** (e.g. mkaiser's `sensor.load_power`): Numbat learns your
  hourly load profile from its history — there is no manual profile to type
  in. Together with the outdoor temperature sensor from step 4
  (`entities.outdoor_temp`), Numbat also learns your house's temperature
  response — how much load heatwaves and cold snaps add — and applies it to
  forecast temperatures. Without a load sensor Numbat still plans, but assumes
  **zero house load** and shows a warning on the dashboard; raise
  `battery.soc_min` to keep a comfort buffer until you can provide one.

## 6. Install the Numbat add-on

1. Settings → Add-ons → Add-on store → ⋮ → Repositories → add
   `https://github.com/danbitzer/numbat` → install **Numbat — Energy Optimizer**.
2. Start the add-on. It boots **unconfigured and disabled** — no planning
   runs yet. Open the **Numbat** sidebar item (ingress) → **Settings**
   and fill in (every field has inline help; entity fields are searchable
   pickers):
   - **Entities** — the entities from steps 2–5.
   - **Battery** — capacity, charge/discharge limits (deliberately cap these
     below your inverter's capability to reduce cell wear; the spike
     discharge cap can raise it during confirmed spikes only), efficiency,
     SoC bounds, wear cost.
   - **Grid connection** — your connection's import limit and DNSP export limit.
   - **Spike strategy** — the spike-reserve hedge; defaults are sane.
3. Flip **Numbat enabled** on, save, and watch the add-on log: you should see
   `cycle ok: action=...` within a minute. The Dashboard view shows the plan
   — prices, PV/load forecast, SoC trajectory.
4. Check Developer tools → States for `sensor.numbat_action`,
   `sensor.numbat_power_setpoint`, `sensor.numbat_soc_target`,
   `sensor.numbat_horizon_cost`, `sensor.numbat_status`.

At this point Numbat is a pure **recommendation engine** — it writes nothing to
the inverter, ever. Run it like this for at least a few days: watch the
dashboard and compare its recommendations against the Amber app and what your
inverter would have done on its own. Only wire up actuation once the plans
consistently look right to you (sensible charge windows, no phantom-spike
chasing, a load forecast that matches your household).

## 7. Actuation

Import [`blueprints/numbat_actuator.yaml`](../blueprints/numbat_actuator.yaml)
(Settings → Automations → Blueprints → Import), create an automation from it,
and fill in the three action sequences — charge / discharge / idle (plus
optional curtail/un-curtail export-limit sequences for negative feed-in) — for your
hardware; a complete Sungrow (mkaiser) example lives in the add-on
Documentation tab ([numbat/DOCS.md](../numbat/DOCS.md)). The blueprint has a
heartbeat failsafe built in: if Numbat stops publishing or reports an error, your
idle sequence runs and the inverter returns to self-consumption.

Bench-test before trusting it: watch a charge → discharge → idle transition,
then stop the add-on and confirm the failsafe fires within its check interval.
Disabling the automation is always the master off-switch.
