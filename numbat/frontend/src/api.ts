// Zod mirror of the add-on's /api/plan payload (see numbat/web/app.py) — the
// single source of truth for the contract: TS types are inferred from the
// schemas, and every response is validated so a drift fails loudly at the
// fetch boundary instead of rendering garbage. The API is unchanged by the
// React migration. Frontend and backend ship in the same image, so strict
// validation carries no version-skew risk.

import { z } from "zod";

export const ActionSchema = z.enum(["charge", "discharge", "idle", "no_charge", "curtail"]);
export type Action = z.infer<typeof ActionSchema>;

export const PlanIntervalSchema = z.object({
  start: z.string(), // ISO datetime
  end: z.string(),
  action: ActionSchema,
  power_kw: z.number(), // +charge / −discharge
  soc_start: z.number(), // kWh
  soc_end: z.number(),
  buy: z.number(), // $/kWh
  sell: z.number(),
  pv_kw: z.number(),
  load_kw: z.number(),
  grid_import_kw: z.number(),
  grid_export_kw: z.number(),
  interval_cost: z.number(),
});
export type PlanInterval = z.infer<typeof PlanIntervalSchema>;

export const LoadForecastInfoSchema = z.looseObject({
  load_entity: z.string().optional(),
  source: z.string().optional(),
  window_days: z.number().optional(),
  hours_used: z.number().optional(),
  buckets: z.string().optional(),
  temp_response: z.boolean().optional(),
  learned_at: z.string().optional(),
  temp_entity: z.string().optional(),
  heat_kw_per_deg: z.number().optional(),
  cool_kw_per_deg: z.number().optional(),
  buffer: z.number().optional(),
});
export type LoadForecastInfo = z.infer<typeof LoadForecastInfoSchema>;

export const VacationInfoSchema = z.object({
  baseline_kw: z.number().nullish(),
  until: z.string().nullish(),
});
export type VacationInfo = z.infer<typeof VacationInfoSchema>;

// "Why this action" — a plain-language explanation of the current interval
// plus the numbers behind it (see numbat/explain.py). Loose sub-objects: the
// backend may add fields, and the fallback path omits context/levers.
export const ExplanationSchema = z.object({
  reason: z.string(),
  values: z.looseObject({
    buy: z.number(),
    sell: z.number(),
    pv_kw: z.number(),
    load_kw: z.number(),
    soc_start_kwh: z.number(),
    soc_end_kwh: z.number(),
    soc_start_pct: z.number().optional(),
    soc_end_pct: z.number().optional(),
    battery_kw: z.number(),
    grid_import_kw: z.number(),
    grid_export_kw: z.number(),
    interval_cost: z.number(),
  }),
  context: z
    .looseObject({
      sell_rank: z.number().optional(),
      buy_rank: z.number().optional(),
      horizon_steps: z.number().optional(),
      hold_value: z.number().optional(),
      flat: z.boolean().optional(),
      hysteresis: z.boolean().optional(),
    })
    .optional(),
  levers: z
    .looseObject({
      spike_reserve: z.object({ kwh: z.number(), until: z.string().nullish() }).nullish(),
      daily_target: z.boolean().optional(),
      live_spike: z.boolean().optional(),
      prices_estimated: z.boolean().optional(),
    })
    .optional(),
  stale: z.boolean().optional(),
});
export type Explanation = z.infer<typeof ExplanationSchema>;

// meta is a loose object: the backend adds informational keys over time and
// the UI should keep working (and keep validating) without listing them all.
export const PlanMetaSchema = z.looseObject({
  capacity_kwh: z.number().optional(),
  price_forecast_end: z.string().nullish(),
  coverage: z.record(z.string(), z.number()).nullish(),
  load_forecast: z.enum(["learned", "pending", "unconfigured"]).optional(),
  load_forecast_info: LoadForecastInfoSchema.optional(),
  vacation: VacationInfoSchema.nullish(),
  prices_estimated: z.boolean().optional(),
  explanation: ExplanationSchema.nullish(),
  // test mode: present on simulated plans (synthetic scenario or time travel)
  simulated: z.boolean().optional(),
  mode: z.string().optional(),
  at: z.string().optional(),
  notes: z.array(z.string()).optional(),
});
export type PlanMeta = z.infer<typeof PlanMetaSchema>;

export const PlanResponseSchema = z.object({
  computed_at: z.string(),
  solver_status: z.string(),
  solve_ms: z.number(),
  objective_cost: z.number(),
  meta: PlanMetaSchema,
  intervals: z.array(PlanIntervalSchema),
});
export type PlanResponse = z.infer<typeof PlanResponseSchema>;

export class PlanError extends Error {}

export async function fetchPlan(): Promise<PlanResponse> {
  // Relative URL: the page lives behind HA ingress at an unpredictable prefix.
  const resp = await fetch("./api/plan", { cache: "no-store" });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = ((await resp.json()) as { error?: string }).error ?? detail;
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new PlanError(detail);
  }
  const parsed = PlanResponseSchema.safeParse(await resp.json());
  if (!parsed.success) {
    throw new PlanError(`plan payload failed validation: ${z.prettifyError(parsed.error)}`);
  }
  return parsed.data;
}

// --- Test mode: run the optimizer against synthetic price scenarios ---

export const ScenarioSchema = z.object({
  id: z.string(),
  label: z.string(),
  description: z.string(),
});
export type Scenario = z.infer<typeof ScenarioSchema>;

export async function fetchScenarios(): Promise<Scenario[]> {
  const resp = await fetch("./api/scenarios", { cache: "no-store" });
  if (!resp.ok) throw new Error(`scenarios failed: ${resp.statusText}`);
  return z.object({ scenarios: z.array(ScenarioSchema) }).parse(await resp.json()).scenarios;
}

async function postSimulation(url: string, body: unknown): Promise<PlanResponse> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (resp.status === 422) {
    // The sandbox config failed pydantic validation — same per-field error
    // shape as PUT /api/config, so the sandbox form can mark the inputs.
    const body422 = (await resp.json()) as { errors: FieldError[] };
    throw new ConfigValidationError(body422.errors);
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = ((await resp.json()) as { error?: string }).error ?? detail;
    } catch {
      // non-JSON error body
    }
    throw new Error(detail);
  }
  const parsed = PlanResponseSchema.safeParse(await resp.json());
  if (!parsed.success) {
    throw new Error(`simulation payload failed validation: ${z.prettifyError(parsed.error)}`);
  }
  return parsed.data;
}

/** The test-mode sandbox: whole config sections (battery/grid/optimizer/spike,
 * /api/config document shape) that replace the live ones for this simulation
 * only. Omitted fields fall back to server defaults, matching the form. */
export type SandboxConfig = Record<string, unknown>;

export async function runSimulation(req: {
  scenario: string;
  soc_frac: number;
  config?: SandboxConfig;
}): Promise<PlanResponse> {
  return postSimulation("./api/simulate", req);
}

/** Time travel: replay the optimizer over recorded HA history from a past
 * instant. `at` is a local datetime-local string; soc_frac null/omitted means
 * "use the battery level recorded at that time". */
export async function runHistorySimulation(req: {
  at: string;
  soc_frac?: number | null;
  config?: SandboxConfig;
}): Promise<PlanResponse> {
  return postSimulation("./api/simulate/history", req);
}

// The /health payload — same body on 200 and 503 (unhealthy), so parse
// without checking resp.ok. The dashboard polls this to surface degraded
// planning: /api/plan keeps serving the last good plan when cycles fail
// (that's the point of the fallback), so plan polling alone can't tell.
export const HealthResponseSchema = z.object({
  healthy: z.boolean(),
  lifecycle: z.string(),
  last_success: z.string().nullable(),
  last_error: z.string(),
});
export type HealthResponse = z.infer<typeof HealthResponseSchema>;

export async function fetchHealth(): Promise<HealthResponse> {
  const resp = await fetch("./health", { cache: "no-store" });
  return HealthResponseSchema.parse(await resp.json());
}

export async function fetchHealthError(): Promise<string> {
  try {
    return (await fetchHealth()).last_error;
  } catch {
    return "";
  }
}

/** fetchPlan with the add-on's last cycle error appended — the message the
 * dashboard shows when a poll fails. Used as the TanStack Query queryFn. */
export async function fetchPlanOrExplain(): Promise<PlanResponse> {
  try {
    return await fetchPlan();
  } catch (e) {
    let msg = `No plan yet: ${e instanceof PlanError ? e.message : String(e)}`;
    const lastError = await fetchHealthError();
    if (lastError) msg += ` — last cycle error: ${lastError}`;
    throw new PlanError(msg);
  }
}

// ---- in-app configuration (issue #5) ----------------------------------------
// The config document itself is intentionally loose here: the server's
// pydantic Settings model is the single source of truth and the validation
// gate; the form reads/writes it via dot paths from the field spec.

export type ConfigDoc = Record<string, unknown>;

export const ConfigResponseSchema = z.object({
  configured: z.boolean(),
  lifecycle: z.enum(["running", "disabled", "unconfigured"]),
  config: z.record(z.string(), z.unknown()).nullable(),
});
export type ConfigResponse = z.infer<typeof ConfigResponseSchema>;

export const EntitySchema = z.object({
  entity_id: z.string(),
  name: z.string(),
  domain: z.string(),
  device_class: z.string().nullish(),
  unit: z.string().nullish(),
});
export type Entity = z.infer<typeof EntitySchema>;

export interface FieldError {
  loc: string; // dot path, e.g. "battery.capacity_kwh"
  msg: string;
}

/** PUT /api/config rejected the document — per-field pydantic errors. */
export class ConfigValidationError extends Error {
  constructor(public fieldErrors: FieldError[]) {
    super("configuration is invalid");
  }
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const resp = await fetch("./api/config", { cache: "no-store" });
  if (!resp.ok) throw new Error(`config fetch failed: ${resp.statusText}`);
  return ConfigResponseSchema.parse(await resp.json());
}

export async function putConfig(doc: ConfigDoc): Promise<void> {
  const resp = await fetch("./api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(doc),
  });
  if (resp.status === 422) {
    const body = (await resp.json()) as { errors: FieldError[] };
    throw new ConfigValidationError(body.errors);
  }
  if (!resp.ok) {
    const detail = ((await resp.json().catch(() => ({}))) as { error?: string }).error;
    throw new Error(detail ?? `config save failed: ${resp.statusText}`);
  }
}

export async function fetchEntities(): Promise<Entity[]> {
  const resp = await fetch("./api/entities", { cache: "no-store" });
  if (!resp.ok) {
    const detail = ((await resp.json().catch(() => ({}))) as { error?: string }).error;
    throw new Error(detail ?? `entity list failed: ${resp.statusText}`);
  }
  return z.object({ entities: z.array(EntitySchema) }).parse(await resp.json()).entities;
}
