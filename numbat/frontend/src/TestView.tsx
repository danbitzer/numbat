import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  ConfigValidationError,
  fetchScenarios,
  runHistorySimulation,
  runSimulation,
  type SandboxConfig,
} from "./api";
import { Button } from "./components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./components/ui/card";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./components/ui/select";
import { PlanView } from "./PlanView";
import {
  mapServerErrors,
  NO_SANDBOX_ERRORS,
  type SandboxErrors,
} from "./settings/form";

/** Yesterday at this time, as a datetime-local value (browser-local). */
function defaultAt(): string {
  const d = new Date(Date.now() - 24 * 3600 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

type Mode = "scenario" | "history";
const MODES: { id: Mode; label: string }[] = [
  { id: "scenario", label: "Scenarios" },
  { id: "history", label: "Time travel" },
];

function SocSlider({
  socPct,
  setSocPct,
  help,
}: {
  socPct: number;
  setSocPct: (v: number) => void;
  help: string;
}) {
  return (
    <div className="space-y-1.5">
      <Label>
        Starting battery <span className="font-mono">{socPct}%</span>
      </Label>
      <input
        type="range"
        min={0}
        max={100}
        step={5}
        value={socPct}
        onChange={(e) => setSocPct(Number(e.target.value))}
        className="accent-primary h-2 w-full cursor-pointer"
      />
      <p className="text-muted-foreground text-xs">{help}</p>
    </div>
  );
}

export interface SimStatus {
  pending: boolean;
  canRun: boolean;
}

export function TestView({
  sandbox,
  sandboxDirty,
  onSandboxErrors,
  registerRun,
  onSimStatus,
}: {
  /** The sandbox config sections sent with every simulation (null until the
   * live config has loaded). */
  sandbox: SandboxConfig | null;
  sandboxDirty: boolean;
  /** Simulate rejected the sandbox config — errors for the settings panel. */
  onSandboxErrors: (errors: SandboxErrors) => void;
  /** Hands the current run-trigger up so the settings panel's Run button can
   * re-run without scrolling back to this card. */
  registerRun: (run: () => void) => void;
  onSimStatus: (status: SimStatus) => void;
}) {
  const scenarios = useQuery({ queryKey: ["scenarios"], queryFn: fetchScenarios });
  const [mode, setMode] = useState<Mode>("scenario");
  const [scenario, setScenario] = useState("");
  const [at, setAt] = useState(defaultAt);
  const [recordedSoc, setRecordedSoc] = useState(true);
  const [socPct, setSocPct] = useState(70);

  useEffect(() => {
    if (!scenario && scenarios.data?.[0]) setScenario(scenarios.data[0].id);
  }, [scenario, scenarios.data]);

  const sim = useMutation({
    mutationFn: () =>
      mode === "scenario"
        ? runSimulation({ scenario, soc_frac: socPct / 100, config: sandbox ?? undefined })
        : runHistorySimulation({
            at,
            soc_frac: recordedSoc ? null : socPct / 100,
            config: sandbox ?? undefined,
          }),
    onSuccess: () => onSandboxErrors(NO_SANDBOX_ERRORS),
    onError: (e) => {
      if (e instanceof ConfigValidationError) {
        const { byField, general } = mapServerErrors(e.fieldErrors);
        onSandboxErrors({ fields: byField, general });
      }
    },
  });

  const chosen = scenarios.data?.find((s) => s.id === scenario);
  const canRun = mode === "scenario" ? Boolean(scenario) : Boolean(at);
  const notes = sim.data?.meta.notes ?? [];

  // Re-register every render (no deps) so the panel's Run always triggers
  // with the CURRENT scenario/time/SoC selections.
  useEffect(() => {
    registerRun(() => sim.mutate());
  });
  useEffect(() => {
    onSimStatus({ pending: sim.isPending, canRun });
  }, [onSimStatus, sim.isPending, canRun]);
  const simError =
    sim.error instanceof ConfigValidationError
      ? "The test settings are invalid — see the errors in the settings panel."
      : sim.error
        ? String(sim.error)
        : "";

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Run a simulation</CardTitle>
          <CardDescription>
            Run the optimiser without waiting for real prices to change — against a made-up
            price scenario, or time-travelled onto data Home Assistant actually recorded.
            Nothing here touches your live plan or the inverter. Every run uses the test
            settings (the ⚙ panel), so you can preview a config change before applying it.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Scenarios | Time travel toggle, styled like the old tab pill */}
          <nav className="flex w-fit gap-[3px] rounded-full bg-tab-bg p-[3px] max-sm:w-full">
            {MODES.map((m) => (
              <button
                key={m.id}
                type="button"
                aria-current={mode === m.id ? "page" : undefined}
                onClick={() => setMode(m.id)}
                className={
                  "cursor-pointer rounded-full border-none px-4 py-[6px] text-[13px] font-semibold transition-all max-sm:flex-1 " +
                  (mode === m.id
                    ? "bg-card text-foreground shadow-[0_1px_2px_rgba(0,0,0,.12)] dark:bg-[#2c2c2c] dark:shadow-none"
                    : "bg-transparent text-muted-foreground hover:text-foreground")
                }
              >
                {m.label}
              </button>
            ))}
          </nav>

          {mode === "scenario" ? (
            <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_220px]">
              <div className="space-y-1.5">
                <Label>Price scenario</Label>
                <Select value={scenario} onValueChange={setScenario}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Choose a scenario…" />
                  </SelectTrigger>
                  <SelectContent>
                    {(scenarios.data ?? []).map((s) => (
                      <SelectItem key={s.id} value={s.id}>
                        {s.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {chosen && (
                  <p className="text-muted-foreground text-xs leading-snug">
                    {chosen.description}
                  </p>
                )}
              </div>
              <SocSlider
                socPct={socPct}
                setSocPct={setSocPct}
                help="Battery state of charge at the start."
              />
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_220px]">
              <div className="space-y-1.5">
                <Label>Go back to</Label>
                <Input
                  type="datetime-local"
                  className="h-auto w-full rounded-md bg-secondary px-[13px] py-2 font-mono text-sm"
                  value={at}
                  onChange={(e) => setAt(e.target.value)}
                />
                <p className="text-muted-foreground text-xs leading-snug">
                  Replays the prices, solar and load Home Assistant recorded from this moment
                  (hindsight, not the forecast Numbat saw). The recorder keeps ~10 days by default.
                </p>
              </div>
              <div className="space-y-2.5">
                <div className="space-y-1.5">
                  <Label className="flex cursor-pointer items-center gap-2">
                    <input
                      type="checkbox"
                      checked={recordedSoc}
                      onChange={(e) => setRecordedSoc(e.target.checked)}
                      className="accent-primary size-4 cursor-pointer"
                    />
                    Use recorded battery level
                  </Label>
                  <p className="text-muted-foreground text-xs">
                    Start from the SoC the battery actually had at that time.
                  </p>
                </div>
                {!recordedSoc && (
                  <SocSlider
                    socPct={socPct}
                    setSocPct={setSocPct}
                    help="Battery state of charge at that moment."
                  />
                )}
              </div>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" disabled={!canRun || sim.isPending} onClick={() => sim.mutate()}>
              {sim.isPending
                ? "Running…"
                : mode === "scenario"
                  ? "Run simulation"
                  : "Replay"}
            </Button>
            {sim.data && (
              <span className="text-muted-foreground text-xs">
                Solved in {Math.round(sim.data.solve_ms)} ms · {sim.data.solver_status}
              </span>
            )}
            {sandboxDirty && (
              <span className="text-muted-foreground text-xs">
                Using test settings that differ from live.
              </span>
            )}
          </div>
          {simError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3.5 py-2.5 text-[13px] text-destructive">
              {simError}
            </div>
          )}
        </CardContent>
      </Card>

      {notes.length > 0 && (
        <div className="rounded-lg border border-border bg-card px-3.5 py-2.5 text-[13px] text-muted-foreground">
          {notes.map((n) => (
            <div key={n} className="flex gap-1.5">
              <span aria-hidden>ℹ︎</span>
              <span>{n}</span>
            </div>
          ))}
        </div>
      )}

      {sim.data ? (
        <PlanView plan={sim.data} />
      ) : (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          {mode === "scenario" ? (
            <>
              Pick a scenario and press <span className="font-medium">Run simulation</span> to see
              the plan.
            </>
          ) : (
            <>
              Pick a moment in the past and press{" "}
              <span className="font-medium">Replay</span> to see how Numbat would have planned it.
            </>
          )}
        </div>
      )}
    </>
  );
}
