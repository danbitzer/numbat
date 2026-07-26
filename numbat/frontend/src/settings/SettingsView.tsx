import { useForm } from "@tanstack/react-form";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  type ConfigResponse,
  ConfigValidationError,
  fetchConfig,
  fetchEntities,
  putConfig,
} from "@/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { refetchPlanUntilFresh } from "@/planRefresh";
import { setThemePref, type ThemePref, useThemePref } from "@/theme";
import { BackupCard } from "./BackupCard";
import { buildDefaults, CollapsibleCard, FieldRow, mapServerErrors, toDoc } from "./form";
import { getPath, SECTIONS } from "./spec";
import { VacationCard } from "./VacationCard";

export function SettingsView() {
  const config = useQuery({ queryKey: ["config"], queryFn: fetchConfig });
  // Bumped after a successful import so the form remounts and rebuilds its
  // defaults from the freshly imported config (useForm captures them once).
  const [formEpoch, setFormEpoch] = useState(0);
  if (config.isPending) return <div className="p-6 text-center">loading…</div>;
  if (config.isError) {
    return <div className="p-6 text-center text-destructive">{String(config.error)}</div>;
  }
  return (
    // w-full matters: with mx-auto the flex parent can't stretch this (auto
    // cross-axis margins disable it), and shrink-to-fit sizing is floored by
    // min-content — one wide entity label would widen everything past the
    // viewport.
    <div className="mx-auto w-full max-w-3xl space-y-4">
      <SettingsForm key={formEpoch} initialConfig={config.data.config} />
      <BackupCard
        configured={config.data.configured}
        onImported={() => setFormEpoch((e) => e + 1)}
      />
    </div>
  );
}

function SettingsForm({ initialConfig }: { initialConfig: Record<string, unknown> | null }) {
  const queryClient = useQueryClient();
  const entities = useQuery({ queryKey: ["entities"], queryFn: fetchEntities, retry: 1 });
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [generalErrors, setGeneralErrors] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  // Section cards start collapsed on a configured install (compact overview)
  // and open on a fresh one (guided setup — every field needs a look).
  const [openSections, setOpenSections] = useState<Record<string, boolean>>(() =>
    initialConfig === null ? Object.fromEntries(SECTIONS.map((s) => [s.id, true])) : {},
  );
  const openSectionsFor = (paths: Iterable<string>) => {
    const ids = new Set([...paths].map((p) => p.split(".")[0] ?? ""));
    if (ids.size) {
      setOpenSections((prev) => {
        const next = { ...prev };
        for (const id of ids) next[id] = true;
        return next;
      });
    }
  };

  const save = useMutation({
    mutationFn: putConfig,
    onSuccess: () => {
      setServerErrors({});
      setGeneralErrors([]);
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ["config"] });
      void refetchPlanUntilFresh(queryClient);
    },
    onError: (e) => {
      setSaved(false);
      if (e instanceof ConfigValidationError) {
        const { byField, general } = mapServerErrors(e.fieldErrors);
        setServerErrors(byField);
        setGeneralErrors(general);
        openSectionsFor(Object.keys(byField)); // an error must never hide in a collapsed card
      } else {
        setGeneralErrors([String(e)]);
      }
    },
  });

  const form = useForm({
    defaultValues: buildDefaults(initialConfig),
    onSubmit: async ({ value }) => {
      setSaved(false);
      const doc = toDoc(value);
      // The vacation section is managed by VacationCard, not this form —
      // carry the last-saved value through so a form save can't reset it.
      const latest = queryClient.getQueryData<ConfigResponse>(["config"]);
      if (latest?.config?.vacation !== undefined) doc.vacation = latest.config.vacation;
      await save.mutateAsync(doc).catch(() => undefined); // surfaced via mutation state
    },
  });

  return (
    <form
      className="space-y-4"
      // The server (pydantic) is the validation authority — native browser
      // validation would reject values it accepts (e.g. step mismatches like
      // a 0.12 daily target against step=0.05) with a bubble instead of our
      // error UI. min/max/step stay on the inputs as spinner hints only.
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        // Client-side "Required" errors render inside the cards — open any
        // section that is about to fail so the message is actually visible.
        openSectionsFor(
          SECTIONS.flatMap((s) => s.fields)
            .filter(
              (f) => f.required && String(getPath(form.state.values, f.path) ?? "").trim() === "",
            )
            .map((f) => f.path),
        );
        void form.handleSubmit();
      }}
    >
      <Card>
        <CardHeader>
          <CardTitle>Numbat enabled</CardTitle>
          <CardDescription>
            The master switch. While off (or before first configuration), Numbat runs no planning
            cycles and publishes <code>sensor.numbat_status</code> as something other than{" "}
            <code>ok</code> — your actuator automation's failsafe then keeps the inverter in
            plain self-consumption.
          </CardDescription>
          <CardAction>
            <form.Field name="enabled">
              {(field) => (
                <Switch
                  checked={field.state.value === true}
                  onCheckedChange={(checked) => field.handleChange(checked)}
                />
              )}
            </form.Field>
          </CardAction>
        </CardHeader>
      </Card>

      <VacationCard />

      {SECTIONS.map((section) => (
        <CollapsibleCard
          key={section.id}
          title={section.title}
          description={section.description}
          open={openSections[section.id] === true}
          onToggle={() =>
            setOpenSections((prev) => ({ ...prev, [section.id]: prev[section.id] !== true }))
          }
        >
          {section.id === "entities" && entities.isError && (
            <p className="text-destructive mb-2 text-xs">
              Entity list unavailable ({String(entities.error)}) — type entity IDs manually.
            </p>
          )}
          <div className="divide-y">
            {section.fields
              .filter((spec) => !spec.testOnly)
              .map((spec) => (
                <form.Field
                  key={spec.path}
                  name={spec.path}
                  validators={{
                    onSubmit: ({ value }) =>
                      spec.required && String(value ?? "").trim() === ""
                        ? "Required"
                        : undefined,
                  }}
                >
                  {(field) => (
                    <FieldRow
                      spec={spec}
                      value={field.state.value as string | boolean}
                      onChange={(v) => {
                        field.handleChange(v);
                        setServerErrors((prev) => {
                          if (!(spec.path in prev)) return prev;
                          const { [spec.path]: _dropped, ...rest } = prev;
                          return rest;
                        });
                      }}
                      error={field.state.meta.errors[0]?.toString() ?? serverErrors[spec.path]}
                      entities={entities.data ?? []}
                    />
                  )}
                </form.Field>
              ))}
          </div>
        </CollapsibleCard>
      ))}

      <AppearanceCard />

      {generalErrors.length > 0 && (
        <div className="border-destructive text-destructive rounded-xl border px-4 py-3 text-sm">
          {generalErrors.map((msg) => (
            <div key={msg}>{msg}</div>
          ))}
        </div>
      )}

      <div className="bg-background/95 sticky bottom-0 flex items-center gap-3 border-t py-3 backdrop-blur">
        <form.Subscribe selector={(s) => s.isSubmitting}>
          {(isSubmitting) => (
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? "Saving…" : "Save & apply"}
            </Button>
          )}
        </form.Subscribe>
        {saved && (
          <span className="text-muted-foreground text-sm">
            Saved — applied before the next cycle.
          </span>
        )}
        {!saved && (Object.keys(serverErrors).length > 0 || generalErrors.length > 0) && (
          <span className="text-destructive text-sm">Not saved — fix the errors above.</span>
        )}
      </div>
    </form>
  );
}

/** Client-side only — applies instantly and is stored in this browser
 * (localStorage), not in the Numbat config, so it sits outside the form/save. */
function AppearanceCard() {
  const pref = useThemePref();
  return (
    <Card>
      <CardHeader className="max-sm:flex max-sm:flex-col max-sm:gap-2">
        <CardTitle>Theme</CardTitle>
        <CardDescription>
          Light, dark, or follow this device's preference. Applies immediately and is
          remembered in this browser only.
        </CardDescription>
        <CardAction className="max-sm:w-full">
          <Select value={pref} onValueChange={(v) => setThemePref(v as ThemePref)}>
            <SelectTrigger className="w-32 max-sm:w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="system">System</SelectItem>
              <SelectItem value="light">Light</SelectItem>
              <SelectItem value="dark">Dark</SelectItem>
            </SelectContent>
          </Select>
        </CardAction>
      </CardHeader>
    </Card>
  );
}
