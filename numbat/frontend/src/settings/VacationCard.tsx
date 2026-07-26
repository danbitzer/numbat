import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type ConfigDoc, ConfigValidationError, fetchConfig, putConfig } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { refetchPlanUntilFresh } from "@/planRefresh";

interface StoredVacation {
  enabled?: boolean;
  baseline_kw?: number;
  until?: string | null;
}

function vacationOf(config: Record<string, unknown> | null): StoredVacation {
  return (config?.vacation as StoredVacation | undefined) ?? {};
}

function isActive(v: StoredVacation): boolean {
  // Naive "until" strings are local time — exactly how Date parses them.
  return v.enabled === true && (!v.until || new Date(v.until) > new Date());
}

export function fmtUntil(until: string | null | undefined): string {
  return until ? `until ${new Date(until).toLocaleString()}` : "until turned off";
}

/** Suggested end: tomorrow at the next full hour, as a datetime-local value.
 * The field is always pre-filled with a CONCRETE value: Safari displays
 * today's date in an untouched datetime-local input while its value is still
 * "" — users saw the picker "set" yet PUT `until: null`. */
export function suggestedUntil(now = new Date()): string {
  const d = new Date(now.getTime() + 24 * 3600_000);
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/** Vacation mode lives outside the main settings form: enabling/disabling is
 * an immediate, targeted PUT of the last-saved config with only `vacation`
 * changed — it must not depend on (or accidentally save) unsaved form edits. */
export function VacationCard() {
  const queryClient = useQueryClient();
  const config = useQuery({ queryKey: ["config"], queryFn: fetchConfig });
  const [open, setOpen] = useState(false);
  const [baseline, setBaseline] = useState<string | null>(null); // null = seed from stored
  const [until, setUntil] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const stored = vacationOf(config.data?.config ?? null);
  const active = isActive(stored);
  const expired = stored.enabled === true && !active;

  const save = useMutation({
    mutationFn: (vacation: StoredVacation) => {
      const base = config.data?.config;
      if (!base) throw new Error("configure Numbat first");
      return putConfig({ ...base, vacation } as ConfigDoc);
    },
    onSuccess: () => {
      setError(null);
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["config"] });
      void refetchPlanUntilFresh(queryClient);
    },
    onError: (e) => {
      setError(
        e instanceof ConfigValidationError
          ? e.fieldErrors.map((f) => `${f.loc}: ${f.msg}`).join("; ")
          : String(e),
      );
    },
  });

  const openDialog = () => {
    setBaseline(null);
    // Editing an existing vacation keeps its end (incl. open-ended);
    // enabling fresh pre-fills the suggestion — never an untouched empty
    // picker, which Safari renders with today's date but values as "".
    setUntil(stored.until ? stored.until.slice(0, 16) : active ? "" : suggestedUntil());
    setError(null);
    setOpen(true);
  };
  const baselineValue = baseline ?? String(stored.baseline_kw ?? 0.3);
  const untilValue = until ?? "";

  return (
    <Card>
      {/* On phones stack title → description → action (the header grid would
          otherwise put the description beside the title and cramp both). */}
      <CardHeader className="max-sm:flex max-sm:flex-col max-sm:gap-2">
        <CardTitle>Vacation mode</CardTitle>
        <CardDescription>
          {active ? (
            <>
              <span className="font-medium text-[#efa63c]">Active</span> — load forecast
              flattened to {stored.baseline_kw} kW, {fmtUntil(stored.until)}.
            </>
          ) : (
            <>
              Household away? Flatten the load forecast to a standby baseline so the whole
              battery is free for the market{expired && " (previous vacation has ended)"}.
            </>
          )}
        </CardDescription>
        <CardAction className="max-sm:w-full">
          <Button
            type="button"
            variant={active ? "default" : "outline"}
            className={
              (active ? "rounded-md" : "rounded-md bg-secondary") +
              " max-sm:h-auto max-sm:w-full max-sm:whitespace-normal max-sm:py-3 max-sm:text-[15px]"
            }
            disabled={!config.data?.configured}
            onClick={openDialog}
          >
            {active
              ? `Vacation mode on · ${stored.until ? `ends ${new Date(stored.until).toLocaleString(undefined, { hour: "numeric", minute: "2-digit", day: "numeric", month: "short" })}` : "no end time"}`
              : "Enable vacation mode…"}
          </Button>
        </CardAction>
        {error && !open && <p className="text-destructive text-xs">{error}</p>}
      </CardHeader>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Vacation mode</DialogTitle>
            <DialogDescription>
              While active, Numbat plans with a flat standby baseline instead of the learned
              load forecast — no temperature response, no load buffer — freeing the battery
              to chase spikes and cheap windows. The plan reverts to the learned forecast
              from the end time onward (or when you disable it), and{" "}
              <code>binary_sensor.numbat_vacation_mode</code> reports the state to Home
              Assistant.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-1">
            <div className="grid gap-1.5">
              <Label htmlFor="vacation-baseline">Baseline load (kW)</Label>
              <Input
                id="vacation-baseline"
                type="number"
                className="w-44"
                min={0}
                step={0.05}
                value={baselineValue}
                onChange={(e) => setBaseline(e.target.value)}
              />
              <p className="text-muted-foreground text-xs">
                Your house's standby draw — fridge, network gear, pumps. Typically 0.2–0.4
                kW; check your load sensor overnight while nothing is running.
              </p>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="vacation-until">Ends</Label>
              <div className="flex flex-wrap items-center gap-2">
                {untilValue ? (
                  <>
                    <Input
                      id="vacation-until"
                      type="datetime-local"
                      className="w-56"
                      value={untilValue}
                      onChange={(e) => setUntil(e.target.value)}
                    />
                    <Button type="button" variant="outline" size="sm" onClick={() => setUntil("")}>
                      No end time
                    </Button>
                  </>
                ) : (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setUntil(suggestedUntil())}
                  >
                    Pick end time
                  </Button>
                )}
              </div>
              {/* What will actually be saved — makes a silently-empty picker
                  (Safari shows today's date at value "") visible before PUT */}
              <p className="text-xs font-medium">
                {untilValue
                  ? `Ends ${new Date(untilValue).toLocaleString()}.`
                  : "No end time — stays on until you disable it."}
              </p>
              <p className="text-muted-foreground text-xs">
                Local time; vacation mode expires on its own — if the end lands inside the
                planning horizon, the plan already covers your return evening.
              </p>
            </div>
            {error && <p className="text-destructive text-xs">{error}</p>}
          </div>
          <DialogFooter>
            {active && (
              <Button
                type="button"
                variant="destructive"
                className="sm:mr-auto"
                disabled={save.isPending}
                onClick={() => save.mutate({ ...stored, enabled: false })}
              >
                Disable vacation mode
              </Button>
            )}
            <DialogClose asChild>
              <Button type="button" variant="ghost">
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="button"
              disabled={save.isPending}
              onClick={() =>
                save.mutate({
                  enabled: true,
                  baseline_kw: Number(baselineValue),
                  until: untilValue || null,
                })
              }
            >
              {save.isPending ? "Saving…" : active ? "Update" : "Enable"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
