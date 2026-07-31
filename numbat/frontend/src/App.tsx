import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, FlaskConical, Settings as SettingsIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  type ConfigResponse,
  fetchConfig,
  fetchHealth,
  fetchPlanOrExplain,
  type HealthResponse,
  type PlanResponse,
} from "./api";
import { installIosScrollKick } from "./iosScrollKick";
import { PLAN_REFRESHING_KEY } from "./planRefresh";
import { PlanView } from "./PlanView";
import {
  buildDefaults,
  type FormValues,
  NO_SANDBOX_ERRORS,
  type SandboxErrors,
  sandboxDoc,
} from "./settings/form";
import { SandboxPanel } from "./settings/SandboxPanel";
import { SettingsView } from "./settings/SettingsView";
import { type SimStatus, TestView } from "./TestView";

const REFRESH_MS = 60_000;

/** Top-level mode, Stripe-style: Live is the real dashboard and settings;
 * Test is the simulation sandbox. Always lands on Live. */
type AppMode = "live" | "test";

/** Debug/screenshot affordance only: `?mode=test&settings=1` pre-sets the UI
 * state. HA ingress never adds query params, so real users always land on
 * Live with the panel closed (or auto-opened by the unconfigured effect). */
function initialUiState(): { mode: AppMode; open: boolean | null } {
  try {
    const p = new URLSearchParams(window.location.search);
    return {
      mode: p.get("mode") === "test" ? "test" : "live",
      open: p.has("settings") ? p.get("settings") !== "0" : null,
    };
  } catch {
    return { mode: "live", open: null };
  }
}

export function App() {
  const [initial] = useState(initialUiState);
  const [mode, setMode] = useState<AppMode>(initial.mode);
  // null = the user hasn't chosen yet (lets the unconfigured effect open it)
  const [openChosen, setOpenChosen] = useState<boolean | null>(initial.open);
  // Polled so the lifecycle banner clears when the main loop flips to
  // running shortly after an enable (the save-triggered refetch can race it).
  const config = useQuery({
    queryKey: ["config"],
    queryFn: fetchConfig,
    refetchInterval: 30_000,
  });
  const plan = useQuery<PlanResponse>({
    queryKey: ["plan"],
    queryFn: fetchPlanOrExplain,
    refetchInterval: REFRESH_MS,
    retry: false,
  });
  // Work around a WKWebView scroll bug in HA's iOS app (ingress iframe won't
  // scroll until it gains focus). Harmless everywhere else. See the module.
  useEffect(() => installIosScrollKick(), []);
  // A fresh install lands with the settings panel open (and it STAYS open —
  // pinned as the user's choice, or the first successful save would slam it
  // shut); once the user toggles the gear, their choice wins.
  useEffect(() => {
    if (openChosen === null && config.data && !config.data.configured) {
      setOpenChosen(true);
    }
  }, [openChosen, config.data]);
  const settingsOpen = openChosen ?? false;

  // The test-mode sandbox: an editable copy of the live config's solver
  // sections. Lifted here so it survives panel toggles and mode switches;
  // created lazily from the live config on first entry into Test.
  const liveConfig = config.data?.config ?? null;
  const [sandboxValues, setSandboxValues] = useState<FormValues | null>(null);
  const [sandboxErrors, setSandboxErrors] = useState<SandboxErrors>(NO_SANDBOX_ERRORS);
  useEffect(() => {
    if (mode === "test" && sandboxValues === null && liveConfig) {
      setSandboxValues(buildDefaults(liveConfig));
    }
  }, [mode, sandboxValues, liveConfig]);
  const sandbox = sandboxValues && liveConfig ? sandboxDoc(sandboxValues, liveConfig) : null;
  const sandboxDirty =
    sandbox !== null &&
    liveConfig !== null &&
    JSON.stringify(sandbox) !== JSON.stringify(sandboxDoc(buildDefaults(liveConfig), liveConfig));

  // Bridge between TestView (which owns the simulation) and the sandbox
  // panel's Run button, so a re-run doesn't require scrolling back to the
  // top of the test column.
  const runSimRef = useRef<() => void>(() => {});
  const [simStatus, setSimStatus] = useState<SimStatus>({ pending: false, canRun: false });

  // Content columns are CSS-hidden rather than unmounted so a mode flip
  // never throws away simulation results or dashboard state — and each column
  // is its own scroll container, so dashboard, test results and settings all
  // keep independent scroll positions. The columns are full-bleed (content
  // centered by an inner wrapper) so their scrollbars sit at the settings
  // divider and the screen edge instead of overlapping the cards.
  // overflow-x-hidden matters: setting overflow-y alone would compute
  // overflow-x to auto, so any 1px-too-wide child adds a sideways scroll.
  const contentCls = (m: AppMode) =>
    "min-w-0 flex-1 overflow-x-hidden overflow-y-auto " +
    (mode !== m ? "hidden" : settingsOpen ? "hidden lg:block" : "block");

  return (
    <div className="flex h-dvh flex-col">
      {/* Full-width app bar: title left, mode switch + gear right */}
      <header className="shrink-0 border-b border-border bg-card px-[22px] py-[18px]">
        <div className="flex w-full flex-wrap items-center justify-between gap-x-4 gap-y-2.5">
          <h1 className="min-w-0 text-[17px] font-bold text-foreground max-sm:w-full">
            Numbat
          </h1>
          <div className="flex shrink-0 items-center gap-2.5 max-sm:w-full">
            <ModeSwitch mode={mode} onChange={setMode} />
            <button
              type="button"
              aria-label={settingsOpen ? "Close settings" : "Open settings"}
              aria-pressed={settingsOpen}
              onClick={() => setOpenChosen(!settingsOpen)}
              className={
                "flex size-9 shrink-0 cursor-pointer items-center justify-center rounded-lg border transition-colors " +
                (settingsOpen
                  ? "border-primary/35 bg-primary/10 text-primary"
                  : "border-border bg-transparent text-muted-foreground hover:text-foreground")
              }
            >
              {settingsOpen ? (
                <>
                  {/* On phones the panel replaces the page, so "close" reads
                      as going back; on wide screens it stays a gear toggle. */}
                  <ArrowLeft className="size-[18px] lg:hidden" />
                  <SettingsIcon className="size-[18px] max-lg:hidden" />
                </>
              ) : (
                <SettingsIcon className="size-[18px]" />
              )}
            </button>
          </div>
        </div>
      </header>
      <main className="flex min-h-0 w-full flex-1 items-stretch">
        <div className={contentCls("live")} data-scrollkick="">
          <div className="mx-auto flex w-full max-w-[960px] flex-col gap-3.5 p-5">
            <Dashboard
              config={config.data}
              plan={plan.data}
              error={plan.error ? plan.error.message : null}
            />
          </div>
        </div>
        <div className={contentCls("test")} data-scrollkick="">
          <div className="mx-auto flex w-full max-w-[960px] flex-col gap-3.5 p-5">
            <TestView
              sandbox={sandbox}
              sandboxDirty={sandboxDirty}
              onSandboxErrors={(errors) => {
                setSandboxErrors(errors);
                // surface the panel the errors point at
                if (errors.general.length || Object.keys(errors.fields).length) {
                  setOpenChosen(true);
                }
              }}
              registerRun={(run) => {
                runSimRef.current = run;
              }}
              onSimStatus={setSimStatus}
            />
          </div>
        </div>
        {settingsOpen && (
          <aside
            className="w-full min-w-0 overflow-x-hidden overflow-y-auto border-border lg:w-[462px] lg:shrink-0 lg:border-l"
            data-scrollkick=""
          >
            <div className="flex flex-col gap-3 px-4 py-5">
            {mode === "live" ? (
              <>
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 px-1">
                  <h2 className="text-[15px] font-bold">Settings</h2>
                </div>
                <SettingsView />
              </>
            ) : (
              <>
                <div className="flex items-center gap-2 px-1">
                  <h2 className="text-[15px] font-bold">Test settings</h2>
                  <span className="rounded bg-[#1a73d9]/10 px-1.5 py-0.5 text-[10px] font-bold tracking-wider text-[#1a73d9] dark:bg-[#5b9bea]/15 dark:text-[#7fb3f5]">
                    SANDBOX
                  </span>
                </div>
                <p className="text-muted-foreground px-1 text-xs">
                  Changes here affect simulations only — nothing reaches your live settings
                  or the battery until you apply them.
                </p>
                {sandboxValues ? (
                  <SandboxPanel
                    values={sandboxValues}
                    onChange={setSandboxValues}
                    errors={sandboxErrors}
                    onErrors={setSandboxErrors}
                    liveConfig={liveConfig}
                    dirty={sandboxDirty}
                    onRun={() => runSimRef.current()}
                    simStatus={simStatus}
                  />
                ) : (
                  <div className="text-muted-foreground rounded-lg border border-dashed border-border p-6 text-center text-sm">
                    Configure Numbat first — the test sandbox starts as a copy of your live
                    settings.
                  </div>
                )}
              </>
            )}
            </div>
          </aside>
        )}
      </main>
    </div>
  );
}

/** The Live | Test segmented switch. Deliberately NOT styled like the old
 * round tab pill: squared segments, a bordered track, and a blue active Test
 * segment so mode reads as a different axis than navigation. */
function ModeSwitch({ mode, onChange }: { mode: AppMode; onChange: (m: AppMode) => void }) {
  const seg =
    "flex flex-1 sm:flex-none cursor-pointer items-center justify-center gap-1.5 rounded-md " +
    "border-none px-3.5 py-[6px] text-[13px] font-semibold transition-colors ";
  const inactive = "bg-transparent text-muted-foreground hover:text-foreground";
  return (
    <div
      role="radiogroup"
      aria-label="Mode"
      className="flex gap-[2px] rounded-lg border border-border bg-tab-bg p-[2px] max-sm:flex-1"
    >
      <button
        type="button"
        role="radio"
        aria-checked={mode === "live"}
        onClick={() => onChange("live")}
        className={
          seg +
          (mode === "live"
            ? "bg-card text-foreground shadow-[0_1px_2px_rgba(0,0,0,.12)] dark:bg-[#2c2c2c] dark:shadow-none"
            : inactive)
        }
      >
        Live
      </button>
      <button
        type="button"
        role="radio"
        aria-checked={mode === "test"}
        onClick={() => onChange("test")}
        className={
          seg + (mode === "test" ? "bg-[#1a73d9] text-white dark:bg-[#1e5fbd]" : inactive)
        }
      >
        <FlaskConical className="size-3.5" aria-hidden />
        Test
      </button>
    </div>
  );
}

function loadForecastLine(plan: PlanResponse): string | null {
  const lf = plan.meta.load_forecast_info;
  if (plan.meta.load_forecast !== "learned" || !lf?.window_days) return null;
  const days = Math.max(1, Math.round(lf.window_days));
  let text =
    `Load forecast: ${days} day${days === 1 ? "" : "s"} of ${lf.source}`;
  if (lf.half_life_days) text += `, recency-weighted (${lf.half_life_days}-day half-life)`;
  if (lf.temp_response) {
    const heat =
      lf.balance_heat_c != null
        ? `heat up to ${lf.heat_kw_per_deg} kW/°C below ${lf.balance_heat_c}°C`
        : `heat up to ${lf.heat_kw_per_deg} kW/°C`;
    const cool =
      lf.balance_cool_c != null
        ? `cool up to ${lf.cool_kw_per_deg} kW/°C above ${lf.balance_cool_c}°C`
        : `cool up to ${lf.cool_kw_per_deg} kW/°C`;
    text += ` · temp response: ${heat}, ${cool}`;
    if (lf.temp_hours) text += ` (fitted on ${lf.temp_hours.toLocaleString()} h)`;
  } else {
    text += " · no temperature response";
  }
  if (lf.buffer) text += ` · +${Math.round(lf.buffer * 100)}% buffer`;
  return text;
}

function warningText(plan: PlanResponse): string | null {
  const status = plan.meta.load_forecast;
  if (!status || status === "learned") return null;
  return status === "unconfigured"
    ? "⚠ Load forecasting unavailable — no house load sensor configured (entities.load_power). " +
        "Plans assume ZERO house load; consider raising battery.soc_min until learning is set up."
    : "⚠ Load forecast not learned yet — waiting for usable history from the load sensor. " +
        "Plans assume ZERO house load; consider raising battery.soc_min until learning kicks in.";
}

function vacationBanner(plan: PlanResponse): string | null {
  const v = plan.meta.vacation;
  if (!v) return null;
  const until = v.until
    ? `until ${new Date(v.until).toLocaleString()}`
    : "until turned off in Settings";
  return `🌴 Vacation mode — load forecast flattened to a ${v.baseline_kw} kW baseline ${until}.`;
}

/** Failing planning cycles are invisible in /api/plan — it keeps serving the
 * last good plan by design — so the dashboard polls /health for them. Shown
 * only while the loop is running: disabled/unconfigured have their own
 * banner, and a stale last_error from before a disable must not resurface. */
function planningErrorBanner(
  health: HealthResponse | undefined,
  plan: PlanResponse | undefined,
): string | null {
  if (!health || health.lifecycle !== "running") return null;
  if (!health.last_error && health.healthy) return null;
  const reason = health.last_error
    ? `the last planning cycle failed: ${health.last_error}`
    : "no planning cycle has succeeded recently";
  const stale = plan
    ? ` Showing the last good plan (computed ${new Date(plan.computed_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}).`
    : "";
  return (
    `⚠ Planning error — ${reason}.` +
    ` Your actuator's failsafe holds the inverter in self-consumption.${stale}`
  );
}

function lifecycleBanner(config: ConfigResponse | undefined): string | null {
  if (!config || config.lifecycle === "running") return null;
  return config.lifecycle === "unconfigured"
    ? "Numbat is not configured yet — no planning cycles run. Open Settings (⚙) to configure and enable it."
    : "Numbat is disabled — no planning cycles run and your actuator's failsafe keeps the inverter " +
        "in self-consumption. Enable it in Settings (⚙).";
}

function Dashboard({
  config,
  plan,
  error,
}: {
  config: ConfigResponse | undefined;
  plan: PlanResponse | undefined;
  error: string | null;
}) {
  // True while a config save waits for the planner's re-solve: the plan on
  // screen is the pre-save one, so grey it out rather than let it read as
  // current (see refetchPlanUntilFresh).
  const replanning =
    useQuery({
      queryKey: PLAN_REFRESHING_KEY,
      queryFn: () => false,
      enabled: false,
      initialData: false,
    }).data === true;
  // 503 (unhealthy) carries the same JSON body, so this succeeds either way;
  // an unreachable add-on is already surfaced by the plan query's error.
  const health = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
    retry: 1,
  });
  const planningError = planningErrorBanner(health.data, plan);
  const banner = lifecycleBanner(config);
  if (!plan) {
    return (
      <div className="flex flex-col gap-3.5">
        {banner && <Banner text={banner} />}
        {error ? (
          <div className="p-6 text-center text-sm text-destructive">{error}</div>
        ) : (
          <div className="p-6 text-center text-muted-foreground">loading…</div>
        )}
      </div>
    );
  }
  return (
    <div
      aria-busy={replanning}
      className={
        "flex flex-col gap-3.5 transition-opacity duration-300 " +
        (replanning ? "pointer-events-none opacity-40" : "")
      }
    >
      {replanning && (
        // Sticks to the top of the dashboard's scroll container so the state
        // is visible wherever the user has scrolled to.
        <div className="pointer-events-none sticky top-1 z-10 -mb-11 flex justify-center">
          <span className="rounded-full bg-foreground/80 px-3.5 py-1.5 text-xs font-semibold text-background shadow-md">
            Re-planning…
          </span>
        </div>
      )}
      {error && <div className="text-xs text-destructive">{error}</div>}
      {planningError && <Banner text={planningError} tone="error" />}
      {banner && <Banner text={banner} />}
      {vacationBanner(plan) && <Banner text={vacationBanner(plan) as string} />}
      {warningText(plan) && <Banner text={warningText(plan) as string} />}
      <PlanView plan={plan} info={loadForecastLine(plan)} />
    </div>
  );
}

function Banner({ text, tone = "warn" }: { text: string; tone?: "warn" | "error" }) {
  return (
    <div
      className={
        "rounded-lg border px-3.5 py-2.5 text-[13px] " +
        (tone === "error"
          ? "border-destructive bg-destructive/10"
          : "border-[#efa63c] bg-[#efa63c]/10")
      }
    >
      {text}
    </div>
  );
}
