/** The Simple view: a style, how long, how smooth, how much FX, and the plan they make as a coloured ribbon. */
import type { MixPlan } from "@/api/mixplan.generated";
import { Slider, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { LENGTHS, type RibbonKind, planRibbon, RIBBON_LABEL, type SimpleSettings, STYLES } from "./styles";

const RIBBON_STYLE: Record<RibbonKind, string> = {
  a: "var(--color-accent)",
  b: "var(--color-info)",
  both: "color-mix(in srgb, var(--color-accent), var(--color-info))",
  swap: "var(--color-stem-bass)",
  fx: "var(--color-section-build)",
  gap: "transparent",
};

const MIN_LABEL_SHARE = 0.05;

/** What plays when, across the same timeline as the waveforms under it. */
export function PlanRibbon({ plan }: { plan: MixPlan }) {
  const { start_beat: a, end_beat: b } = plan.window;
  const pieces = planRibbon(plan);
  return (
    <div data-testid="plan-ribbon" role="img" aria-label={pieces.map((p) => RIBBON_LABEL[p.kind]).join(", then ")} className="flex h-7 w-full overflow-hidden rounded-sm border border-line bg-bg">
      {pieces.map((p) => {
        const share = (p.to - p.from) / (b - a || 1);
        return (
          <div key={p.from} data-kind={p.kind} title={`${RIBBON_LABEL[p.kind]}: beats ${p.from} to ${p.to}`} className="flex items-center justify-center overflow-hidden" style={{ width: `${share * 100}%`, background: RIBBON_STYLE[p.kind] }}>
            {share >= MIN_LABEL_SHARE && p.kind !== "gap" && <span className="truncate px-1 text-2xs font-semibold text-white">{RIBBON_LABEL[p.kind]}</span>}
          </div>
        );
      })}
    </div>
  );
}

export interface SimpleControlsProps {
  settings: SimpleSettings;
  /** A change while a slider is held comes with `held`; `onRelease` ends it. */
  onChange: (next: SimpleSettings, held: boolean) => void;
  onRelease: () => void;
}

export function SimpleControls({ settings, onChange, onRelease }: SimpleControlsProps) {
  const style = STYLES.find((s) => s.id === settings.style)!;
  const lengthIndex = LENGTHS.reduce((best, n, i) => (Math.abs(n - settings.bars) < Math.abs(LENGTHS[best] - settings.bars) ? i : best), 0);
  return (
    <div className="flex flex-col gap-3">
      <div role="radiogroup" aria-label="Style" className="flex flex-wrap gap-1.5">
        {STYLES.map((s) => (
          <Tooltip key={s.id} content={s.hint}>
            <button
              type="button"
              role="radio"
              aria-checked={s.id === settings.style}
              onClick={() => onChange({ ...settings, style: s.id, bars: s.bars }, false)}
              className={cn(
                "h-control-sm rounded-sm border px-3 text-xs font-medium transition-colors",
                s.id === settings.style ? "border-accent bg-accent-soft text-fg" : "border-line-strong text-fg-muted hover:bg-hover hover:text-fg",
              )}
            >
              {s.label}
            </button>
          </Tooltip>
        ))}
      </div>
      <p className="text-xs text-fg-subtle">{style.hint}</p>
      <div className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-3">
        <Amount label="Length" value={settings.bars === 1 ? "1 bar" : `${settings.bars} bars`}>
          <Slider label="Length" min={0} max={LENGTHS.length - 1} step={1} value={lengthIndex} onChange={(i) => onChange({ ...settings, bars: LENGTHS[i] }, true)} onCommit={onRelease} />
        </Amount>
        <Amount label="Smooth" value={`${Math.round(settings.smooth * 100)}%`}>
          <Slider label="Smooth" step={0.05} value={settings.smooth} onChange={(smooth) => onChange({ ...settings, smooth }, true)} onCommit={onRelease} />
        </Amount>
        <Amount label="FX" value={`${Math.round(settings.fx * 100)}%`}>
          <Slider label="FX" step={0.05} value={settings.fx} onChange={(fx) => onChange({ ...settings, fx }, true)} onCommit={onRelease} />
        </Amount>
      </div>
    </div>
  );
}

function Amount({ label, value, children }: { label: string; value: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between text-xs">
        <span className="text-fg-muted">{label}</span>
        <span className="num text-fg-subtle">{value}</span>
      </div>
      {children}
    </div>
  );
}
