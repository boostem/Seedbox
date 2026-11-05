/**
 * The plan being edited, on a running engine. A/B between the plan as it came and the user's edit is just a `schedule`, as
 * is every edit: the engine takes them while it plays, so the music carries on at the same beat.
 *
 * Undo and redo go by gesture: a drag between `begin` and `end` is one step, however many moves it schedules.
 */
import type { MixPlan } from "@/api/mixplan.generated";

export type PlanSide = "auto" | "edit";

export interface PlanSink {
  schedule(plan: MixPlan): void;
}

const MAX_UNDO = 200;

export class PlanSession {
  side: PlanSide = "auto";
  edited: MixPlan | null = null;
  /** Counts the changes a view has to show: not every move of a drag. */
  revision = 0;
  private undoStack: (MixPlan | null)[] = [];
  private redoStack: (MixPlan | null)[] = [];
  private gesture: { stepped: boolean; showEach: boolean } | null = null;
  private readonly listeners = new Set<() => void>();

  constructor(
    private readonly sink: PlanSink,
    readonly auto: MixPlan,
    edited: MixPlan | null = null,
  ) {
    if (edited) {
      this.edited = edited;
      this.side = "edit";
    }
  }

  get plan(): MixPlan {
    return this.side === "edit" && this.edited ? this.edited : this.auto;
  }

  get canUndo(): boolean {
    return this.undoStack.length > 0;
  }

  get canRedo(): boolean {
    return this.redoStack.length > 0;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private changed(): void {
    this.revision++;
    for (const l of this.listeners) l();
  }

  private step(): void {
    this.undoStack.push(this.edited);
    if (this.undoStack.length > MAX_UNDO) this.undoStack.shift();
    this.redoStack = [];
  }

  /**
   * The edits up to `end` are one undo step. A pointer drag repaints its own row, so views hear only at the end; with
   * `showEach` (a slider that moves in steps, and shows on the ribbon and the waveforms) they hear about every edit.
   */
  begin(showEach = false): void {
    if (!this.gesture) this.gesture = { stepped: false, showEach };
  }

  end(): void {
    const g = this.gesture;
    this.gesture = null;
    if (g?.stepped) this.changed();
  }

  /** The user changed the plan: it's what plays now. */
  edit(plan: MixPlan): void {
    const g = this.gesture;
    if (!g || !g.stepped) this.step();
    if (g) g.stepped = true;
    this.edited = plan;
    this.side = "edit";
    this.sink.schedule(plan);
    if (!g || g.showEach) this.changed();
  }

  setSide(side: PlanSide): void {
    if (side === this.side || (side === "edit" && !this.edited)) return;
    this.side = side;
    this.sink.schedule(this.plan);
    this.changed();
  }

  private restore(edited: MixPlan | null): void {
    this.edited = edited;
    this.side = edited ? "edit" : "auto";
    this.sink.schedule(this.plan);
    this.changed();
  }

  undo(): void {
    const previous = this.undoStack.pop();
    if (previous === undefined) return;
    this.redoStack.push(this.edited);
    this.restore(previous);
  }

  redo(): void {
    const next = this.redoStack.pop();
    if (next === undefined) return;
    this.undoStack.push(this.edited);
    this.restore(next);
  }

  /** Back to the plan as it came. It's an edit like any other: undo brings the edit back. */
  reset(): void {
    if (!this.edited) return;
    this.step();
    this.restore(null);
  }
}
