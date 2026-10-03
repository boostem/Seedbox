/**
 * v4 mix-plan endpoints (APP_SPEC.md "v4"). Kept in the feature until api/client.ts grows them; uses the shared
 * `request` so errors/toasts/mock routing behave like every other call.
 */
import { request, type RequestOptions } from "@/api/client";
import type { MixPlan, MixRender, MixTechnique } from "@/api/types";

const enc = encodeURIComponent;
/** `scope` is a crate folder, or "setlist:<id>" for a setlist's crate-to-crate link (folder names never hold ":"). */
const base = (scope: string, from: string, to: string) =>
  scope.startsWith("setlist:")
    ? `/api/setlists/${enc(scope.slice(8))}/mix/${enc(from)}/${enc(to)}`
    : `/api/crates/${enc(scope)}/mix/${enc(from)}/${enc(to)}`;

export const mixApi = {
  /** Saved edits when present (and no technique/bars given), else a freshly generated plan. */
  plan(folder: string, from: string, to: string, gen: { technique?: MixTechnique; bars?: number } = {}, opts?: RequestOptions): Promise<MixPlan> {
    const q = new URLSearchParams();
    if (gen.technique) q.set("technique", gen.technique);
    if (gen.bars) q.set("bars", String(gen.bars));
    const qs = q.toString();
    return request<MixPlan>("GET", `${base(folder, from, to)}${qs ? `?${qs}` : ""}`, undefined, opts);
  },
  save: (folder: string, from: string, to: string, plan: MixPlan, opts?: RequestOptions) => request<MixPlan>("PUT", base(folder, from, to), plan, opts),
  reset: (folder: string, from: string, to: string, opts?: RequestOptions) => request<MixPlan>("DELETE", base(folder, from, to), undefined, opts),
  /** Render (cached server-side by plan hash). Pass the plan to render unsaved edits. */
  render: (folder: string, from: string, to: string, plan?: MixPlan, opts?: RequestOptions) =>
    request<MixRender>("POST", `${base(folder, from, to)}/render`, plan, opts),
};
