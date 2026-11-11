import { Play } from "lucide-react";
import { useMemo, useRef } from "react";
import { player } from "@/audio/player";
import { Badge, Button, Page, PageBody, PageHeader, Panel, PanelHeader } from "@/components/ui";
import { MixChecks } from "@/features/mix";
import { StemViewTabs, StemWaveform, TrackWaveform, WaveModeTabs, WaveOverview, WavePlaceholder } from "@/features/waveform";
import { demoAnalysis, demoClip, demoRegions } from "@/lib/demo/analysis";
import { DemoTrackTable } from "@/lib/demo/tracks";
import { demoMixChecks } from "@/lib/demo/mixChecks";
import { DEMO_HANDOFF_SPAN, DEMO_HANDOFF_START, demoHandoff } from "@/lib/demo/stems";
import { toneWav } from "@/lib/demo/tone";
import { toast } from "@/store/toasts";

// class names spelled out: Tailwind only generates classes (and emits theme variables) it finds whole in the source
const COLOURS: [string, [string, string][]][] = [
  ["Surfaces", [["bg", "bg-bg"], ["panel", "bg-panel"], ["surface", "bg-surface"], ["raised", "bg-raised"], ["overlay", "bg-overlay"], ["line", "bg-line"], ["line-strong", "bg-line-strong"]]],
  ["Text", [["fg", "bg-fg"], ["fg-muted", "bg-fg-muted"], ["fg-subtle", "bg-fg-subtle"], ["fg-faint", "bg-fg-faint"]]],
  ["Accent and status", [["accent", "bg-accent"], ["accent-hover", "bg-accent-hover"], ["ok", "bg-ok"], ["warn", "bg-warn"], ["danger", "bg-danger"], ["info", "bg-info"]]],
  ["Sections", [["intro / outro", "bg-section-intro"], ["build", "bg-section-build"], ["drop", "bg-section-drop"], ["breakdown", "bg-section-breakdown"], ["body", "bg-section-body"]]],
  ["Phrases, cues, stems", [["phrase", "bg-phrase"], ["cue-mix", "bg-cue-mix"], ["stem-drums", "bg-stem-drums"], ["stem-bass", "bg-stem-bass"], ["stem-vocals", "bg-stem-vocals"], ["stem-other", "bg-stem-other"]]],
  ["Hot cues A-H", [["cue-1", "bg-cue-1"], ["cue-2", "bg-cue-2"], ["cue-3", "bg-cue-3"], ["cue-4", "bg-cue-4"], ["cue-5", "bg-cue-5"], ["cue-6", "bg-cue-6"], ["cue-7", "bg-cue-7"], ["cue-8", "bg-cue-8"]]],
  ["Transition styles", [["style-blend", "bg-style-blend"], ["style-swap", "bg-style-swap"], ["style-filter", "bg-style-filter"], ["style-echo", "bg-style-echo"], ["style-cut", "bg-style-cut"]]],
  ["Waveform", [["wave-low", "bg-wave-low"], ["wave-mid", "bg-wave-mid"], ["wave-high", "bg-wave-high"], ["wave-played", "bg-wave-played"], ["playhead", "bg-playhead"]]],
];

const CONTROLS = [["sm", "h-control-sm"], ["md", "h-control-md"], ["lg", "h-control-lg"], ["xl", "h-control-xl"]] as const;
const ELEVATION = [["card", "shadow-card"], ["pop", "shadow-pop"], ["modal", "shadow-modal"]] as const;

const SIZES = [["2xs", "text-2xs"], ["xs", "text-xs"], ["sm", "text-sm"], ["base", "text-base"], ["md", "text-md"], ["lg", "text-lg"], ["xl", "text-xl"], ["2xl", "text-2xl"]] as const;

/** The design tokens (src/index.css) and the waveform, on synthetic data. */
export function UiPage() {
  const analysis = useMemo(demoAnalysis, []);
  const handoff = useMemo(demoHandoff, []);
  const clip = useMemo(demoClip, []);
  const clipUrl = useRef<string | null>(null);
  const playClip = () => {
    clipUrl.current ??= URL.createObjectURL(toneWav(20));
    void player.play("demo-clip", clipUrl.current, { title: "Preview clip" });
  };
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
            <TrackWaveform analysis={analysis} trackId="demo" deck hotkeys regions={demoRegions()} onSeek={(t) => player.seek(t)} toolbar={<WaveModeTabs />} />
            <div className="mt-4 mb-1.5 text-xs text-fg-subtle">Overviews (library rows, decks): the same renderer</div>
            <div className="flex flex-col gap-1.5">
              {[28, 36, 22].map((h) => (
                <WaveOverview key={h} analysis={analysis} trackId="demo" height={h} sections={h > 24} onSeek={(t) => player.seek(t)} />
              ))}
            </div>
            <div className="mt-4 mb-1.5 flex items-center gap-2 text-xs text-fg-subtle">
              Preview clip (20 s): the same strip
              <Button size="sm" icon={Play} onClick={playClip}>
                Play the clip
              </Button>
            </div>
            <div className="max-w-xs">
              <WaveOverview wave={clip} trackId="demo-clip" onSeek={(t) => player.seek(t)} />
            </div>
          </div>
        </Panel>
        <Panel>
          <PanelHeader title="Stem waveforms" description="features/waveform: a synthetic bass hand-off, each stem dimmed by its gain in the plan" />
          <div className="flex items-center gap-2 px-4 pt-4">
            <StemViewTabs />
            <Button icon={Play} onClick={handoff.play}>
              Play the hand-off
            </Button>
          </div>
          <div className="grid grid-cols-2 gap-4 p-4">
            {handoff.decks.map((d) => (
              <div key={d.label} className="flex flex-col gap-1.5">
                <div className="text-xs text-fg-subtle">{d.label}</div>
                <StemWaveform stems={d.stems} clock={handoff.clock} gainsAt={d.gainsAt} start={DEMO_HANDOFF_START} span={DEMO_HANDOFF_SPAN} height={124} />
                <StemWaveform stems={d.stems} clock={handoff.clock} gainsAt={d.gainsAt} height={32} legend={false} />
              </div>
            ))}
          </div>
        </Panel>
        <Panel>
          <PanelHeader title="Virtualised table" description="10,000 rows; only the visible ones are in the DOM (components/ui/Virtual.tsx)" />
          <DemoTrackTable count={10000} className="h-80" />
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
        <Panel className="max-w-3xl">
          <PanelHeader title="Waiting for a waveform" description="features/waveform: WavePlaceholder, for every view that draws one" />
          <div className="grid grid-cols-2 gap-3 p-4">
            <WavePlaceholder state="loading" seed={2} className="h-24" />
            <WavePlaceholder state="queued" seed={3} className="h-24" />
            <WavePlaceholder state="analysing" seed={4} className="h-24" />
            <WavePlaceholder state="failed" seed={5} className="h-24" message="ffmpeg couldn't read this file" onRetry={() => toast.info("Retried")} />
          </div>
        </Panel>
        <Panel className="max-w-md">
          <PanelHeader title="Mix checks" description="features/mix: mixmetrics checks of a rendered transition, for the transition editor (#79)" />
          <MixChecks checks={demoMixChecks} onFix={(c) => toast.info(`Fix "${c.label}" lands with the editor (#79)`)} />
        </Panel>
      </PageBody>
    </Page>
  );
}
