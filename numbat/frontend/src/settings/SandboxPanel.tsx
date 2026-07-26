// Test-mode sandbox settings: the solver-relevant config sections (battery,
// grid, optimizer, spike) as an editable copy of the live config. Edits apply
// to simulations only — every test run sends the sandbox sections with the
// request — until the user explicitly promotes them with "Apply to live".
// State lives in App (not here) so it survives panel toggles and mode switches.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  type ConfigResponse,
  ConfigValidationError,
  fetchConfig,
  fetchEntities,
  putConfig,
} from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { refetchPlanUntilFresh } from "@/planRefresh";
import {
  buildDefaults,
  CollapsibleCard,
  FieldRow,
  type FormValues,
  mapServerErrors,
  NO_SANDBOX_ERRORS,
  SANDBOX_SECTION_IDS,
  type SandboxErrors,
  sandboxDoc,
} from "./form";
import { ALL_FIELDS, getPath, SECTIONS, setPath } from "./spec";

const SANDBOX_SECTIONS = SECTIONS.filter((s) =>
  (SANDBOX_SECTION_IDS as readonly string[]).includes(s.id),
);

/** Test-only entity settings (spec `testOnly`), shown here instead of the
 * live Entities section. NOT part of the sandbox: simulations always read
 * entities from the live config, so edits save to live immediately. */
function TimeTravelCard() {
  const queryClient = useQueryClient();
  const entities = useQuery({ queryKey: ["entities"], queryFn: fetchEntities, retry: 1 });
  const config = useQuery({ queryKey: ["config"], queryFn: fetchConfig });
  const [open, setOpen] = useState(false);
  // Optimistic display while the save round-trips (the config refetch is
  // what makes the new value stick).
  const [pending, setPending] = useState<Record<string, string>>({});

  const save = useMutation({
    mutationFn: async ({ path, value }: { path: string; value: string }) => {
      const live = queryClient.getQueryData<ConfigResponse>(["config"])?.config;
      if (!live) throw new Error("live config unavailable");
      const doc = structuredClone(live) as Record<string, unknown>;
      setPath(doc, path, value);
      await putConfig(doc);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["config"] });
      setPending({});
    },
    onError: () => setPending({}),
  });

  const liveConfig = config.data?.config ?? null;
  const fields = ALL_FIELDS.filter((f) => f.testOnly);
  if (fields.length === 0) return null;

  return (
    <CollapsibleCard
      title="Time travel data"
      description={
        "Where historical replays get their real data. Unlike the sandbox " +
        "sections above, choosing a sensor here saves to your live settings " +
        "immediately."
      }
      open={open}
      onToggle={() => setOpen((v) => !v)}
    >
      <div className="divide-y">
        {fields.map((spec) => (
          <FieldRow
            key={spec.path}
            spec={spec}
            value={pending[spec.path] ?? String(getPath(liveConfig, spec.path) ?? "")}
            onChange={(v) => {
              setPending((prev) => ({ ...prev, [spec.path]: String(v) }));
              save.mutate({ path: spec.path, value: String(v) });
            }}
            error={save.isError ? String(save.error) : undefined}
            entities={entities.data ?? []}
          />
        ))}
      </div>
      {save.isPending && <p className="text-muted-foreground text-xs">Saving…</p>}
      {save.isSuccess && !save.isPending && (
        <p className="text-muted-foreground text-xs">Saved to live settings.</p>
      )}
    </CollapsibleCard>
  );
}

export function SandboxPanel({
  values,
  onChange,
  errors,
  onErrors,
  liveConfig,
  dirty,
  onRun,
  simStatus,
}: {
  values: FormValues;
  onChange: (v: FormValues) => void;
  /** Validation errors from the last simulate or apply call (422). */
  errors: SandboxErrors;
  onErrors: (e: SandboxErrors) => void;
  liveConfig: Record<string, unknown> | null;
  /** Sandbox differs from a fresh copy of the live config (computed in App —
   * it disables Reset/Apply when there is nothing to reset or apply). */
  dirty: boolean;
  /** Re-run the current simulation (TestView owns it) so tweak-and-compare
   * loops don't require scrolling back to the top of the test column. */
  onRun: () => void;
  simStatus: { pending: boolean; canRun: boolean };
}) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [applied, setApplied] = useState(false);
  const [applyError, setApplyError] = useState("");
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({});
  // Errors must never hide behind a collapsed card: whenever field errors
  // land (from a simulate or apply 422), open the sections holding them.
  const erroredSections = Object.keys(errors.fields)
    .map((p) => p.split(".")[0] ?? "")
    .sort()
    .join(",");
  useEffect(() => {
    if (!erroredSections) return;
    setOpenSections((prev) => {
      const next = { ...prev };
      for (const id of erroredSections.split(",")) next[id] = true;
      return next;
    });
  }, [erroredSections]);

  const apply = useMutation({
    mutationFn: async () => {
      const live = queryClient.getQueryData<ConfigResponse>(["config"])?.config;
      if (!live) throw new Error("live config unavailable");
      // The full live document with only the sandbox sections replaced —
      // entities, vacation, enabled etc. stay exactly as saved.
      await putConfig({ ...live, ...sandboxDoc(values, live) });
    },
    onSuccess: () => {
      setApplied(true);
      setApplyError("");
      onErrors(NO_SANDBOX_ERRORS);
      void queryClient.invalidateQueries({ queryKey: ["config"] });
      void refetchPlanUntilFresh(queryClient);
    },
    onError: (e) => {
      if (e instanceof ConfigValidationError) {
        // Same field/general mapping the live settings form uses, so the
        // rejected inputs light up instead of a bare "invalid" banner.
        const { byField, general } = mapServerErrors(e.fieldErrors);
        onErrors({ fields: byField, general });
        setApplyError("Not applied — fix the errors above.");
      } else {
        setApplyError(String(e));
      }
    },
  });

  const edit = (path: string, v: string | boolean) => {
    const next = structuredClone(values);
    setPath(next, path, v);
    onChange(next);
    setApplied(false);
    if (path in errors.fields) {
      const { [path]: _dropped, ...fields } = errors.fields;
      onErrors({ ...errors, fields });
    }
  };

  return (
    <div className="space-y-4">
      {SANDBOX_SECTIONS.map((section) => (
        <CollapsibleCard
          key={section.id}
          title={section.title}
          description={section.description}
          open={openSections[section.id] === true}
          onToggle={() =>
            setOpenSections((prev) => ({ ...prev, [section.id]: prev[section.id] !== true }))
          }
        >
          <div className="divide-y">
            {section.fields.map((spec) => (
              <FieldRow
                key={spec.path}
                spec={spec}
                value={getPath(values, spec.path) as string | boolean}
                onChange={(v) => edit(spec.path, v)}
                error={errors.fields[spec.path]}
                entities={[]}
              />
            ))}
          </div>
        </CollapsibleCard>
      ))}

      <TimeTravelCard />

      {(errors.general.length > 0 || applyError) && (
        <div className="border-destructive text-destructive rounded-xl border px-4 py-3 text-sm">
          {errors.general.map((msg) => (
            <div key={msg}>{msg}</div>
          ))}
          {applyError && <div>{applyError}</div>}
        </div>
      )}

      <div className="bg-background/95 sticky bottom-0 border-t py-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            disabled={!simStatus.canRun || simStatus.pending}
            onClick={onRun}
          >
            {simStatus.pending ? "Running…" : "Run"}
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={!dirty}
            onClick={() => {
              if (liveConfig) onChange(buildDefaults(liveConfig));
              onErrors(NO_SANDBOX_ERRORS);
              setApplied(false);
              setApplyError("");
            }}
          >
            Reset to live
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={!dirty || apply.isPending}
            onClick={() => setConfirming(true)}
          >
            {apply.isPending ? "Applying…" : "Apply to live"}
          </Button>
          {applied && <span className="text-muted-foreground text-sm">Applied to live settings.</span>}
          {!applied && dirty && (
            <span className="text-muted-foreground text-xs">Differs from live</span>
          )}
        </div>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Apply to live settings?</DialogTitle>
            <DialogDescription>
              The battery, grid, optimizer and spike sections of your live settings will be
              overwritten with the sandbox values, and planning picks them up immediately.
              Entities and everything else stay as saved.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => {
                apply.mutate();
                setConfirming(false);
              }}
            >
              Apply
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
