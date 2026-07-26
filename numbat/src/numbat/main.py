"""Entrypoint: 5-minute cycle scheduler + health/ingress web server.

Each cycle runs the full planner pipeline (gather -> solve -> publish -> record).
A WebSocket watcher triggers an early re-solve on ANY change of the Amber
price sensors (value, estimate flag, or spike status) — that's how a
confirmed price or spike reaches the plan within seconds instead of at the
next 5-minute tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import signal
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import uvicorn

from numbat import __version__
from numbat.adapters.amber import AmberExpressAdapter
from numbat.adapters.solar import OpenMeteoSolarAdapter
from numbat.adapters.sungrow import SungrowAdapter
from numbat.adapters.weather import WeatherAdapter
from numbat.config import EnvSettings, Settings, resolve_connection, resolve_log_level
from numbat.config_store import ConfigController, ConfigStore, resolve_config_path
from numbat.forecast.load import build_load_forecaster, default_timezone
from numbat.ha.client import HaClient
from numbat.ha.publisher import Publisher
from numbat.models import Plan
from numbat.optimizer.model import SolverError
from numbat.planner import InputsStale, Planner
from numbat.web.app import AppState, create_app

log = logging.getLogger("numbat")

CYCLE_SECONDS = 300
WEB_PORT = 8099
EVENT_DEBOUNCE_S = 2  # buy+sell arrive together; just soak up that burst
# Floor between EVENT-driven re-solves (boundary solves are unaffected, and
# events never floor against a boundary solve): a solve is ~50 ms, so this is
# not about cost — it stops a flapping sensor (e.g. unavailable churn) from
# spinning the loop. Kept small: an interval typically produces two closely
# spaced events (estimate roll, then confirmation seconds later) and the
# confirming solve must not be held back — "the plan reflects the confirmed
# price within seconds" is the whole point.
MIN_EVENT_SOLVE_GAP_S = 5
WS_RECONNECT_BACKOFF_S = 30


def seconds_to_next_boundary(now_epoch: float, period: int = CYCLE_SECONDS) -> float:
    """Seconds until the next wall-clock multiple of `period` (min 1s)."""
    return max(1.0, period - (now_epoch % period))


class PriceWatcher:
    """Triggers an asyncio.Event on ANY change of a watched price/spike
    sensor — value or estimate flag. Every solve should reflect the live
    price (a solve is ~50 ms; hysteresis stops action flapping), and an
    estimate->confirmed flip at the SAME value must still re-solve so the
    dashboard's "unconfirmed price" marker clears."""

    def __init__(self, settings: Settings):
        ent = settings.entities
        self.watched = {e for e in (ent.buy_price, ent.sell_price, ent.price_spike) if e}
        self.trigger = asyncio.Event()
        self._last_seen: dict[str, str] = {}

    @staticmethod
    def _key(state: str, attrs: dict | None) -> str:
        # spike_status included because _spike_active() treats
        # spike_status == "spike" as live even while the binary state is
        # still "off" — that flip alone must re-solve too.
        a = attrs or {}
        return f"{state}|{a.get('estimate')}|{a.get('spike_status')}"

    def on_change(
        self,
        entity_id: str,
        new_state: str,
        old_state: str | None = None,
        new_attrs: dict | None = None,
        old_attrs: dict | None = None,
    ) -> None:
        # seed from the event's own old_state so the FIRST change after a
        # (re)connect can still trigger — a spike confirming minutes after an
        # add-on restart must not wait for the 5-min boundary
        key = self._key(new_state, new_attrs)
        old_key = self._key(old_state, old_attrs) if old_state is not None else None
        last = self._last_seen.get(entity_id) or old_key
        self._last_seen[entity_id] = key
        if last is None or last == key:
            return  # attribute-noise (e.g. forecast list refresh) or unseeded
        log.info("price event: %s %s -> %s; early re-solve", entity_id, last, key)
        self.trigger.set()

    async def run(self, client: HaClient) -> None:
        while True:
            try:
                await client.watch_states(self.watched, self.on_change)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - reconnect forever; polling still works
                log.warning("price watcher down (%s); retry in %ss", e, WS_RECONNECT_BACKOFF_S)
                await asyncio.sleep(WS_RECONNECT_BACKOFF_S)


async def cycle(
    planner: Planner,
    publisher: Publisher,
    settings: Settings,
    app_state: AppState,
) -> Plan:
    now = datetime.now(UTC)
    data = await planner.gather(now)
    # The solve is synchronous CVXPY/HiGHS — run off the event loop so /health,
    # the WS watcher, and the dashboard stay responsive during long solves.
    fallback = False
    try:
        plan = await asyncio.to_thread(planner.optimize, data, now)
    except SolverError as e:
        # a re-raise here means no previous plan to reuse either — that IS a
        # failed cycle (degraded status via the caller's handler)
        log.error("solver failed: %s; falling back to the previous plan", e)
        plan = planner.fallback(now)
        fallback = True
    planner.previous_plan = plan
    step0 = plan.intervals[0]
    forecast_end = data.price_forecast_end.isoformat() if data.price_forecast_end else None
    # plan and meta go to the dashboard together — meta lagging the plan gave
    # first-poll renders no capacity axis and no load-forecast warning
    app_state.plan = plan
    app_state.meta = {
        "capacity_kwh": settings.battery.capacity_kwh,
        "price_forecast_end": forecast_end,
        "coverage": data.coverage,
        "load_forecast": data.load_forecast_status,
        "load_forecast_info": data.load_forecast_info,
        "vacation": data.vacation,
        # plain-language "why this action" for the dashboard (see numbat.explain)
        "explanation": plan.explanation,
        # step-0 prices are Amber's estimate, not yet AEMO-confirmed — the
        # dashboard marks the price tile; the estimate->confirmed sensor
        # update triggers a re-solve that clears this within seconds. On a
        # solver-failure fallback the tile shows the PREVIOUS plan's prices,
        # so the marker keeps the flag from the solve those prices came from.
        "prices_estimated": (
            app_state.meta.get("prices_estimated", False)
            if fallback
            else data.prices.current_estimate
        ),
    }

    # Publishing IS the output: the user's actuator automation (see
    # blueprints/numbat_actuator.yaml) turns these sensors into inverter control.
    await publisher.publish_plan(plan, settings.battery.capacity_kwh)
    await publisher.publish_vacation(data.vacation)
    await publisher.publish_status(
        "ok",
        last_solve=now,
        solve_ms=plan.solve_ms,
        extra={
            "coverage": data.coverage,
            "price_forecast_end": forecast_end,
            "load_forecast": data.load_forecast_status,
        },
    )

    log.info(
        "cycle ok: action=%s power=%+.2fkW soc=%.0f%% cost=$%.2f solve=%.0fms",
        step0.action.value,
        step0.power_kw,
        100 * data.battery.soc_frac,
        plan.objective_cost,
        plan.solve_ms,
    )
    return plan


def build_planner(settings: Settings, client: HaClient, tz: ZoneInfo) -> Planner:
    return Planner(
        settings,
        prices=AmberExpressAdapter(client, settings.entities),
        solar=OpenMeteoSolarAdapter(client, settings.entities),
        battery=SungrowAdapter(client, settings.entities, settings.battery),
        weather=WeatherAdapter(client, settings.entities),
        tz=tz,
        load_forecaster=build_load_forecaster(
            client,
            settings.entities.load_power,
            tz,
            outdoor_temp=settings.entities.outdoor_temp,
        ),
    )


async def _run_planner(
    settings: Settings,
    client: HaClient,
    publisher: Publisher,
    tz: ZoneInfo,
    app_state: AppState,
    controller: ConfigController,
) -> None:
    """Build components for the current config and run cycles until the config
    changes (hot-apply: the caller re-reads controller.current and rebuilds)."""
    planner = build_planner(settings, client, tz)
    watcher = PriceWatcher(settings)
    watcher_task = asyncio.create_task(watcher.run(client))

    # Flap floor bookkeeping: EVENT-driven solves only. Floored against the
    # boundary solve it would delay the routine estimate->confirmed re-solve
    # (which lands seconds after every boundary) by the full floor.
    last_event_solve = -math.inf
    try:
        while not controller.changed.is_set():
            try:
                async with asyncio.timeout(90):
                    await cycle(planner, publisher, settings, app_state)
                app_state.health.mark_success()
            except asyncio.CancelledError:
                raise
            except InputsStale as e:
                log.warning("degraded: %s", e)
                app_state.health.mark_error(str(e))
                with contextlib.suppress(Exception):
                    await publisher.publish_status("degraded", detail=str(e))
            except Exception as e:  # noqa: BLE001 - cycle must never kill the loop
                log.exception("cycle failed")
                app_state.health.mark_error(str(e))
                with contextlib.suppress(Exception):
                    await publisher.publish_status("degraded", detail=str(e))

            # Wake on: the 5-min boundary, any price change, or a config
            # change from the Settings UI — whichever comes first.
            delay = seconds_to_next_boundary(time.time())
            price_wait = asyncio.create_task(watcher.trigger.wait())
            config_wait = asyncio.create_task(controller.changed.wait())
            try:
                done, _ = await asyncio.wait(
                    {price_wait, config_wait},
                    timeout=delay,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (price_wait, config_wait):
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
            if config_wait in done:
                return
            # Clear observed-or-stale triggers BEFORE any sleep so a boundary
            # timeout during the debounce can't leave the event set and cause
            # a spurious extra re-solve.
            watcher.trigger.clear()
            if price_wait in done:
                await asyncio.sleep(EVENT_DEBOUNCE_S)  # coalesce price bursts
                # Flap floor between event-driven solves — but never sleep
                # past the 5-min boundary (the event solve then doubles as
                # the boundary solve instead of delaying it).
                gap = MIN_EVENT_SOLVE_GAP_S - (time.monotonic() - last_event_solve)
                gap = min(gap, seconds_to_next_boundary(time.time()))
                if gap > 0:
                    await asyncio.sleep(gap)
                last_event_solve = time.monotonic()
    finally:
        watcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher_task


async def run() -> None:
    env = EnvSettings()  # NUMBAT_* env vars, plus ./.env in dev
    logging.basicConfig(
        level=resolve_log_level(env).upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    conn = resolve_connection(env)
    store = ConfigStore(resolve_config_path(env.config_file))
    controller = ConfigController(store, store.load())
    log.info("Numbat v%s starting (api=%s, config=%s)", __version__, conn.rest_url, store.path)

    if os.environ.get("SUPERVISOR_TOKEN"):
        log.info("dashboard: HA sidebar -> Energy Manager (ingress)")
    else:
        log.info("dashboard: http://localhost:%d", WEB_PORT)

    app_state = AppState()

    try:
        async with HaClient(conn) as client:
            web_task = asyncio.create_task(_serve_web(app_state, controller, client))

            # uvicorn's serve() captures SIGTERM/SIGINT and RE-RAISES them with
            # default handlers after its graceful stop — killing the process
            # before our finally blocks can run. Own handlers, installed after
            # the web task starts, win: they cancel this task so shutdown is
            # clean and logged.
            loop = asyncio.get_running_loop()
            main_task = asyncio.current_task()
            assert main_task is not None
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, main_task.cancel)

            try:
                if not await client.api_ok():
                    log.warning("Home Assistant API not reachable yet; will retry each cycle")
                publisher = Publisher(client)
                tz = default_timezone(env.tz)
                # Anchors every local-time feature (load buckets, daily SoC
                # target, vacation end times) — worth one loud line.
                log.info("local timezone: %s", tz)
                while True:
                    # No await between clear() and the current read: a PUT
                    # landing after the read re-sets the event and is seen by
                    # the next is_set()/wait().
                    controller.changed.clear()
                    settings = controller.current
                    if settings is None or not settings.enabled:
                        status = "unconfigured" if settings is None else "disabled"
                        if app_state.lifecycle != status:
                            log.info(
                                "%s — no planning cycles; sensor.numbat_status=%r keeps the "
                                "actuator's failsafe in self-consumption. Configure and "
                                "enable Numbat in the dashboard's Settings view.",
                                status,
                                status,
                            )
                        app_state.lifecycle = status
                        # Republish each pass: keeps the heartbeat fresh and a
                        # non-ok status in front of the blueprint even across
                        # HA restarts (REST sensors are ephemeral).
                        with contextlib.suppress(Exception):
                            await publisher.publish_status(
                                status, detail="configure and enable Numbat in the web UI"
                            )
                        with contextlib.suppress(TimeoutError):
                            async with asyncio.timeout(CYCLE_SECONDS):
                                await controller.changed.wait()
                        continue
                    if app_state.lifecycle != "running":
                        app_state.health.restart_grace()
                    app_state.lifecycle = "running"
                    await _run_planner(settings, client, publisher, tz, app_state, controller)
                    log.info("configuration changed; applying")
            finally:
                web_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await web_task
    except asyncio.CancelledError:
        log.info("shutting down (signal received)")


async def _serve_web(app_state: AppState, controller: ConfigController, client: HaClient) -> None:
    """Run the dashboard; planning must survive its failure (e.g. port bound —
    uvicorn raises SystemExit(3) inside the task)."""
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(app_state, controller, client),
            host="0.0.0.0",
            port=WEB_PORT,
            log_level="warning",
        )
    )
    try:
        await server.serve()
        log.warning("web server exited; dashboard/health unavailable")
    except asyncio.CancelledError:
        server.should_exit = True
        raise
    except (SystemExit, Exception) as e:  # noqa: BLE001 - dashboard is not load-bearing
        log.error("web server failed (%s); continuing without dashboard/health", e)
