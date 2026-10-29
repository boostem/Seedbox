import { useMemo } from "react";
import { player } from "@/audio/player";
import { Badge, Button, Page, PageBody, PageHeader, Panel, PanelHeader } from "@/components/ui";
import { TrackWaveform } from "@/features/waveform";
import { demoAnalysis } from "@/lib/demo/analysis";

// class names spelled out: Tailwind only generates classes (and emits theme variables) it finds whole in the source
const COLOURS: [string, [string, string][]][] = [
  ["Surfaces", [["bg", "bg-bg"], ["panel", "bg-panel"], ["surface", "bg-surface"], ["raised", "bg-raised"], ["overlay", "bg-overlay"], ["line", "bg-line"], ["line-strong", "bg-line-strong"]]],
  ["Text", [["fg", "bg-fg"], ["fg-muted", "bg-fg-muted"], ["fg-subtle", "bg-fg-subtle"], ["fg-faint", "bg-fg-faint"]]],
  ["Accent and status", [["accent", "bg-accent"], ["accent-hover", "bg-accent-hover"], ["ok", "bg-ok"], ["warn", "bg-warn"], ["danger", "bg-danger"], ["info", "bg-info"]]],
  ["Sections", [["intro / outro", "bg-section-intro"], ["build", "bg-section-build"], ["drop", "bg-section-drop"], ["breakdown", "bg-section-breakdown"], ["body", "bg-section-body"]]],
  ["Phrases, cues, stems", [["phrase", "bg-phrase"], ["cue-mix", "bg-cue-mix"], ["stem-drums", "bg-stem-drums"], ["stem-bass", "bg-stem-bass"], ["stem-vocals", "bg-stem-vocals"], ["stem-other", "bg-stem-other"]]],
  ["Waveform", [["wave-low", "bg-wave-low"], ["wave-mid", "bg-wave-mid"], ["wave-high", "bg-wave-high"], ["wave-played", "bg-wave-played"], ["playhead", "bg-playhead"]]],
];

const CONTROLS = [["sm", "h-control-sm"], ["md", "h-control-md"], ["lg", "h-control-lg"], ["xl", "h-control-xl"]] as const;
const ELEVATION = [["card", "shadow-card"], ["pop", "shadow-pop"], ["modal", "shadow-modal"]] as const;

const SIZES = [["2xs", "text-2xs"], ["xs", "text-xs"], ["sm", "text-sm"], ["base", "text-base"], ["md", "text-md"], ["lg", "text-lg"], ["xl", "text-xl"], ["2xl", "text-2xl"]] as const;

/** The design tokens (src/index.css) and the waveform, on synthetic data. */
export function UiPage() {
  const analysis = useMemo(demoAnalysis, []);
  return (
    <Page>
      <PageHeader title="Design system" subtitle="Design language v2: the tokens in src/index.css, documented in DESIGN2.md" />
      <PageBody className="flex flex-col gap-4">
        <Panel>
          <PanelHeader title="Colour" />
          <div className="grid grid-cols-6 gap-4 p-4">
            {COLOURS.map(([group, names]) => (
              <div key={group}>
                <div className="label-caps mb-2">{group}</div>
                {names.map(([n, cls]) => (
                  <div key={n} className="flex items-center gap-2 py-0.5 text-sm">
                    <span className={`size-4 rounded-xs border border-line-strong ${cls}`} />
                    <span className="font-mono text-xs text-fg-muted">{n}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Panel>
        <Panel>
          <PanelHeader title="Type and controls" />
          <div className="flex items-end gap-5 px-4 pt-4">
            {SIZES.map(([name, cls]) => (
              <span key={name} className={cls}>
                {name}
              </span>
            ))}
          </div>
          <div className="flex items-center gap-2 p-4">
            <Button variant="primary">Primary</Button>
            <Button>Secondary</Button>
            <Button variant="ghost">Ghost</Button>
            <Badge tone="ok">ok</Badge>
            <Badge tone="warn">warn</Badge>
            <Badge tone="danger">danger</Badge>
            <Badge tone="accent">accent</Badge>
          </div>
        </Panel>
        <Panel>
          <PanelHeader title="Waveform" description="features/waveform on a synthetic 128 BPM track" />
          <div className="p-4">
            <TrackWaveform analysis={analysis} trackId="demo" hotkeys onSeek={(t) => player.seek(t)} />
          </div>
        </Panel>
        <Panel>
          <PanelHeader title="Sizes and elevation" description="Control heights 24 / 28 / 32 / 40 px; hover a card for the base motion" />
          <div className="flex items-end gap-6 p-4">
            {CONTROLS.map(([name, cls]) => (
              <div key={name} className="flex flex-col items-center gap-1.5">
                <div className={`${cls} flex w-16 items-center justify-center rounded-sm border border-line-strong bg-raised text-xs text-fg-muted`}>{name}</div>
              </div>
            ))}
            {ELEVATION.map(([name, cls]) => (
              <div
                key={name}
                className={`${cls} ml-2 flex h-16 w-28 items-center justify-center rounded-lg bg-overlay text-xs text-fg-muted transition-transform duration-slow ease-spring hover:-translate-y-1`}
              >
                {name}
              </div>
            ))}
          </div>
        </Panel>
      </PageBody>
    </Page>
  );
}
