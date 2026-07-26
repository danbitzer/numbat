"""Ingress web app.

/health for the Supervisor watchdog, /api/plan for the latest plan,
/api/config + /api/entities for the in-app Settings view, and the built React
dashboard (hem/frontend, built by Vite into web/dist). All URLs must stay
relative so the page works unchanged behind HA ingress.

Auth note: ingress is HA-session-authenticated, so any logged-in HA user who
can open the panel can edit the config — same trust level as the dashboard
itself, acceptable for a household add-on (see DOCS).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from hem import __version__
from hem.config import EnvSettings, Settings
from hem.config_store import SCHEMA_VERSION, ConfigController
from hem.forecast.load import default_timezone
from hem.ha.client import HaClient
from hem.models import Plan
from hem.simulate import SCENARIOS, run_simulation, scenario_list
from hem.time_travel import run_history_simulation

# The config sections test mode's sandbox settings may replace for a
# simulation. Everything else (entities, load learning, vacation, enabled)
# always comes from the live config — it either doesn't affect a sim or
# must not be spoofable from the sandbox.
SANDBOX_SECTIONS = ("battery", "grid", "optimizer", "spike")


def _sandbox_settings(current: Settings, sandbox: object) -> Settings:
    """Test-mode settings: the sandbox sections merged over the live config.

    `sandbox` is the request's optional "config" value — section-shaped like
    the /api/config document; a section that is present REPLACES the live one
    (omitted fields fall back to pydantic defaults, exactly matching what the
    sandbox form displays). Raises ValidationError for the endpoint to shape
    into the same per-field 422 the settings form uses."""
    if not isinstance(sandbox, dict) or not sandbox:
        return current
    doc = current.model_dump(mode="json")
    for section in SANDBOX_SECTIONS:
        if section in sandbox:
            doc[section] = sandbox[section]
    return Settings.model_validate(doc)


def _resolve_tz() -> ZoneInfo:
    try:
        return default_timezone(EnvSettings().tz)
    except Exception:  # noqa: BLE001 - a bad HEM_TZ shouldn't 500 the tool
        return ZoneInfo("UTC")


def _opt_float(v: Any) -> float | None:
    """Parse an optional numeric override; blank/None/invalid -> None (use saved)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

HEALTHY_WINDOW = timedelta(minutes=15)
# Vite build output (hem/frontend -> `bun run build`); gitignored, built by CI
# before the image build and shipped inside the package.
DIST_DIR = Path(__file__).parent / "dist"


@dataclass
class HealthState:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_success: datetime | None = None
    last_error: str = ""

    def mark_success(self) -> None:
        self.last_success = datetime.now(UTC)
        self.last_error = ""

    def mark_error(self, error: str) -> None:
        self.last_error = error

    def restart_grace(self) -> None:
        """Re-arm the startup grace period. Called when planning (re)starts
        after a disabled/unconfigured stretch: last_success is stale from
        before the pause, and without a fresh window the watchdog would see
        503 — and restart the add-on — the moment the user enables HEM."""
        self.started_at = datetime.now(UTC)
        self.last_success = None
        self.last_error = ""

    @property
    def healthy(self) -> bool:
        # Grace period after startup so the watchdog doesn't kill us before the
        # first cycle completes.
        ref = self.last_success or self.started_at
        return datetime.now(UTC) - ref < HEALTHY_WINDOW


@dataclass
class AppState:
    health: HealthState = field(default_factory=HealthState)
    plan: Plan | None = None
    # cycle metadata for the dashboard: capacity_kwh, price_forecast_end, coverage
    meta: dict = field(default_factory=dict)
    # "running" | "disabled" | "unconfigured" — set by the main loop. While
    # not running there are deliberately no cycles, so /health must NOT go
    # unhealthy (the Supervisor watchdog would restart-loop a disabled add-on).
    lifecycle: str = "running"


def _validation_errors(e: ValidationError) -> list[dict[str, str]]:
    """Pydantic errors as per-field entries the form can attach to inputs."""
    return [
        {"loc": ".".join(str(part) for part in err["loc"]), "msg": err["msg"]}
        for err in e.errors()
    ]


def create_app(
    state: AppState,
    controller: ConfigController | None = None,
    client: HaClient | None = None,
    dist_dir: Path = DIST_DIR,
) -> FastAPI:
    app = FastAPI(title="HEM", version=__version__)
    health = state.health

    @app.get("/api/plan")
    async def api_plan() -> JSONResponse:
        if state.plan is None:
            return JSONResponse({"error": "no plan computed yet"}, status_code=404)
        plan = state.plan
        return JSONResponse(
            {
                "computed_at": plan.computed_at.isoformat(),
                "solver_status": plan.solver_status,
                "solve_ms": plan.solve_ms,
                "objective_cost": plan.objective_cost,
                "meta": state.meta,
                "intervals": [
                    {
                        "start": iv.start.isoformat(),
                        "end": iv.end.isoformat(),
                        "action": iv.action.value,
                        "power_kw": iv.power_kw,
                        "soc_start": iv.soc_start,
                        "soc_end": iv.soc_end,
                        "buy": iv.buy,
                        "sell": iv.sell,
                        "pv_kw": iv.pv_kw,
                        "load_kw": iv.load_kw,
                        "grid_import_kw": iv.grid_import_kw,
                        "grid_export_kw": iv.grid_export_kw,
                        "interval_cost": iv.interval_cost,
                    }
                    for iv in plan.intervals
                ],
            }
        )

    @app.get("/health")
    async def health_endpoint() -> JSONResponse:
        healthy = health.healthy or state.lifecycle in ("disabled", "unconfigured")
        body = {
            "healthy": healthy,
            "lifecycle": state.lifecycle,
            "last_success": health.last_success.isoformat() if health.last_success else None,
            "last_error": health.last_error,
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    if controller is not None:

        @app.get("/api/config")
        async def get_config() -> JSONResponse:
            current = controller.current
            return JSONResponse(
                {
                    "configured": current is not None,
                    "lifecycle": state.lifecycle,
                    "config": current.model_dump(mode="json") if current else None,
                }
            )

        @app.get("/api/config/export")
        async def export_config() -> JSONResponse:
            """The saved config as a file download — the exact document shape
            of the on-disk hem-config.json, so an exported file, a filesystem
            backup, and the .bak are all interchangeable for import."""
            current = controller.current
            if current is None:
                return JSONResponse({"error": "configure HEM first"}, status_code=409)
            stamp = datetime.now(_resolve_tz()).strftime("%Y-%m-%d")
            return JSONResponse(
                {"schema_version": SCHEMA_VERSION, "config": current.model_dump(mode="json")},
                headers={
                    "Content-Disposition": f'attachment; filename="hem-config-{stamp}.json"'
                },
            )

        @app.put("/api/config")
        async def put_config(request: Request) -> JSONResponse:
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "request body is not valid JSON"}, 400)
            try:
                settings = Settings.model_validate(body)
            except ValidationError as e:
                return JSONResponse({"errors": _validation_errors(e)}, status_code=422)
            try:
                controller.apply(settings)
            except OSError as e:
                return JSONResponse({"error": f"could not write the config file: {e}"}, 500)
            return JSONResponse({"ok": True, "config": settings.model_dump(mode="json")})

        @app.get("/api/scenarios")
        async def get_scenarios() -> JSONResponse:
            return JSONResponse({"scenarios": scenario_list()})

        @app.post("/api/simulate")
        async def simulate(request: Request) -> JSONResponse:
            """Run the optimizer against a synthetic price scenario (test mode).
            Read-only: never publishes sensors or touches the live plan."""
            current = controller.current
            if current is None:
                return JSONResponse({"error": "configure HEM first"}, status_code=409)
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "request body is not valid JSON"}, 400)
            scenario = body.get("scenario")
            if scenario not in SCENARIOS:
                return JSONResponse({"error": f"unknown scenario: {scenario!r}"}, 400)
            soc = _opt_float(body.get("soc_frac"))
            try:
                settings = _sandbox_settings(current, body.get("config"))
            except ValidationError as e:
                return JSONResponse({"errors": _validation_errors(e)}, status_code=422)
            try:
                result = await asyncio.to_thread(
                    run_simulation,
                    settings,
                    scenario_id=scenario,
                    soc_frac=soc if soc is not None else 0.5,
                    now=datetime.now(UTC),
                    tz=_resolve_tz(),
                )
            except Exception as e:  # noqa: BLE001 - report solver/setup failures to the UI
                return JSONResponse({"error": f"simulation failed: {e}"}, status_code=500)
            return JSONResponse(result)

        @app.post("/api/simulate/history")
        async def simulate_history(request: Request) -> JSONResponse:
            """Time travel: replay the optimizer over recorded HA history from
            a chosen past instant. Read-only, like the synthetic scenarios."""
            if client is None:
                return JSONResponse(
                    {"error": "time travel needs a Home Assistant connection"}, 503
                )
            current = controller.current
            if current is None:
                return JSONResponse({"error": "configure HEM first"}, status_code=409)
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "request body is not valid JSON"}, 400)
            try:
                at = datetime.fromisoformat(str(body.get("at")))
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "'at' must be an ISO datetime, e.g. 2026-07-20T17:30"}, 400
                )
            soc = _opt_float(body.get("soc_frac"))
            try:
                settings = _sandbox_settings(current, body.get("config"))
            except ValidationError as e:
                return JSONResponse({"errors": _validation_errors(e)}, status_code=422)
            try:
                result = await run_history_simulation(
                    settings,
                    client,
                    at=at,
                    soc_frac=soc,
                    wall_now=datetime.now(UTC),
                    tz=_resolve_tz(),
                )
            except ValueError as e:  # user-facing validation (bad time, no data)
                return JSONResponse({"error": str(e)}, status_code=400)
            except Exception as e:  # noqa: BLE001 - report replay failures to the UI
                return JSONResponse({"error": f"time travel failed: {e}"}, status_code=500)
            return JSONResponse(result)

    if client is not None:

        @app.get("/api/entities")
        async def get_entities() -> JSONResponse:
            """Everything the entity pickers need; the frontend filters by
            domain/device_class. Friendly names beat raw entity IDs."""
            try:
                states = await client.list_states()
            except Exception as e:  # noqa: BLE001 - HA down is a soft failure here
                return JSONResponse({"error": f"Home Assistant unreachable: {e}"}, 502)
            entities: list[dict[str, Any]] = [
                {
                    "entity_id": s.entity_id,
                    "name": s.attributes.get("friendly_name") or s.entity_id,
                    "domain": s.entity_id.split(".", 1)[0],
                    "device_class": s.attributes.get("device_class"),
                    "unit": s.attributes.get("unit_of_measurement"),
                }
                for s in sorted(states, key=lambda s: s.entity_id)
            ]
            return JSONResponse({"entities": entities})

    @app.middleware("http")
    async def cache_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Vite assets are content-hashed -> cache forever; index.html points
        AT those hashes, so it must revalidate every load or an add-on update
        keeps serving the previous build until a force-refresh."""
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"  # ETag revalidation, cheap 304s
        return response

    if (dist_dir / "index.html").exists():
        # Registered after the API routes, so those match first. html=True
        # serves index.html at "/" and the hashed Vite assets relative to it.
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="dashboard")
    else:
        # Dev checkout without a built frontend: keep the API useful and say
        # exactly what is missing instead of a bare 404.
        @app.get("/")
        async def index() -> JSONResponse:
            return JSONResponse(
                {"error": "dashboard not built — run `bun run build` in hem/frontend"},
                status_code=503,
            )

    return app
