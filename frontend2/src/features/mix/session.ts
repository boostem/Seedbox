/**
 * A/B between the plan as it came (the auto plan, or the saved one) and the user's edit, on a running engine: a side is
 * just a `schedule`, and the engine takes that while it plays, so the music carries on at the same beat.
 */
import type { MixPlan } from "@/api/mixplan.generated";

export type PlanSide = "auto" | "edit";

export interface PlanSink {
  schedule(plan: MixPlan): void;
}

export class PlanSession {
  side: PlanSide = "auto";
  edited: MixPlan | null = null;
  private readonly listeners = new Set<() => void>();

  constructor(
    private readonly sink: PlanSink,
    readonly auto: MixPlan,
  ) {}

  get plan(): MixPlan {
    return this.side === "edit" && this.edited ? this.edited : this.auto;
  }

  /** Coarse changes only (the side, whether there's an edit): a drag doesn't call this per move. */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const l of this.listeners) l();
  }

  /** The user changed the plan: it's what plays now. */
  edit(plan: MixPlan): void {
    const coarse = this.side !== "edit" || this.edited === null;
    this.edited = plan;
    this.side = "edit";
    this.sink.schedule(plan);
    if (coarse) this.emit();
  }

  setSide(side: PlanSide): void {
    if (side === this.side || (side === "edit" && !this.edited)) return;
    this.side = side;
    this.sink.schedule(this.plan);
    this.emit();
  }

  /** Throws the edit away. */
  reset(): void {
    if (!this.edited) return;
    const wasEdit = this.side === "edit";
    this.edited = null;
    this.side = "auto";
    if (wasEdit) this.sink.schedule(this.auto);
    this.emit();
  }
}
