/** The mix view's details panel: how to mix the pair (or the analysis behind the plan) and its warnings. */
import { AlertTriangle } from "lucide-react";
import { cn } from "@/lib/cn";
import type { MixGeometry } from "@/features/waveform";
import type { SetTransition } from "./model";

export function MixNotes({ t, hasPlan, g }: { t: SetTransition; hasPlan: boolean; g: MixGeometry | null }) {
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <section className="rounded-md border border-line bg-surface px-3 py-2.5">
        <h3 className="label-caps mb-1.5 !text-fg-muted">{hasPlan ? "Transition analysis" : "How to mix it"}</h3>
        <ol className="flex flex-col gap-1.5 text-sm select-text">
          {t.notes.map((n, i) => (
            <li key={i} className={cn("flex gap-2", i === 0 && t.ready ? "font-medium text-fg" : "text-fg-muted")}>
              <span className="mt-[7px] size-1 shrink-0 rounded-full bg-fg-faint" />
              {n}
            </li>
          ))}
          {t.pending && <li className="text-fg-subtle">Scoring this pair…</li>}
        </ol>
      </section>
      <section className="rounded-md border border-line bg-surface px-3 py-2.5">
        <h3 className="label-caps mb-1.5 !text-fg-muted">Warnings</h3>
        {t.warnings.length ? (
          <ul className="flex flex-col gap-1.5 text-sm select-text">
            {t.warnings.map((w, i) => (
              <li key={i} className="flex gap-2 text-warn">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                {w}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-fg-subtle">{t.ready ? "None — this one should be clean." : "—"}</p>
        )}
        {g && g.clash.length > 0 && <p className="mt-2 text-xs text-fg-subtle">Red bars in the ruler = both decks carry vocals on that bar.</p>}
      </section>
    </div>
  );
}
