import { useEffect, useMemo } from "react";
import { player, usePlayer } from "@/audio/player";
import { Page, PageBody, PageHeader, Panel, PanelHeader } from "@/components/ui";
import { headTrace, traceJitter } from "@/audio/playhead";
import { TrackWaveform, type WaveFollow, waveRendererInfo, waveSettings } from "@/features/waveform";
import { demoAnalysis } from "@/lib/demo/analysis";
import { toneWav } from "@/lib/demo/tone";
import { DemoTrackTable } from "@/lib/demo/tracks";
import { demoSet } from "@/lib/demo/setTimeline";
import { layoutTimeline } from "@/features/sets/timeline/layout";
import { SetPlayback } from "@/features/sets/timeline/playback";
import { SetTimeline } from "@/features/sets/timeline/SetTimeline";

/** `window.__seedboxBench`: what `python -m seedbox2.bench frontend` drives over the DevTools protocol. */
export interface BenchHooks {
  ready: true;
  /** Plays a quiet tone as long as the demo track, so the playhead moves and the waveform follows it. Resolves to
   * the player's status ("playing", or "error" when the browser can't play). */
  play: (startAt?: number) => Promise<string>;
  stop: () => void;
  /** How the waveform follows the playhead. */
  follow: (mode: WaveFollow) => void;
  /** Records the playhead every frame; `traceStop` says how even it was, in ms. */
  traceStart: () => void;
  traceStop: () => ReturnType<typeof traceJitter>;
  /** What draws the waveforms: "webgl2 (<GPU>)" or "canvas2d". */
  gpu: () => string;
  /** The audio engine's benchmark: `python -m seedbox2.bench engine`. */
  engine: (seconds?: number, offTempo?: boolean) => Promise<unknown>;
  /** A real engine on this server, for tests/test_seedbox2_engine_browser.py to drive. */
  liveEngine: () => Promise<unknown>;
}

declare global {
  interface Window {
    __seedboxBench?: BenchHooks;
  }
}

const NO_PINS = new Set<number>();
const noop = () => {};

/** A 30-track set, every track with its own waveform: as much as a long night puts on screen at once. */
function BenchTimeline() {
  const { layout, wave, playback } = useMemo(() => {
    const set = demoSet(30);
    const layout = layoutTimeline(set.order, [], set.data);
    const playback = new SetPlayback({ engine: () => Promise.reject(new Error("not in the benchmark")), plan: () => Promise.reject(new Error("not in the benchmark")) });
    playback.setLayout(layout);
    return { layout, wave: (hash: string) => set.waves.get(hash) ?? null, playback };
  }, []);
  return <SetTimeline layout={layout} arc={[[0, 0.5], [0.7, 1], [1, 0.75]]} onArc={noop} drawing={false} reorderable pinned={NO_PINS} onReorder={noop} open={null} onOpen={noop} playback={playback} waves={wave} />;
}

/** #/bench: the waveform, the whole-set timeline and a 10,000-row virtualised table, for the frontend benchmark (not in
 * the sidebar). The stem lanes (#78) join this page when they land. */
export function BenchPage() {
  const analysis = useMemo(demoAnalysis, []);
  useEffect(() => {
    let url: string | null = null;
    window.__seedboxBench = {
      ready: true,
      play: async (startAt = 0) => {
        url ??= URL.createObjectURL(toneWav(analysis.duration_s));
        await player.play("demo", url, { startAt });
        return usePlayer.getState().status;
      },
      stop: () => player.stop(),
      follow: waveSettings.setFollow,
      traceStart: () => {
        headTrace.frames = [];
        headTrace.clock = [];
        headTrace.head = [];
        headTrace.on = true;
      },
      traceStop: () => {
        headTrace.on = false;
        return traceJitter(headTrace);
      },
      gpu: waveRendererInfo,
      engine: async (seconds, offTempo) => (await import("@/audio/engine/bench")).engineBench(seconds, offTempo),
      liveEngine: async () => new (await import("@/audio/engine")).LiveEngine(),
    };
    return () => {
      player.stop();
      if (url) URL.revokeObjectURL(url);
      delete window.__seedboxBench;
    };
  }, [analysis]);
  return (
    <Page>
      <PageHeader title="Benchmark" subtitle="Driven by python -m seedbox2.bench frontend" />
      <PageBody>
        <Panel>
          <PanelHeader title="Waveform" />
          <div className="p-4" data-bench="waveform">
            <TrackWaveform analysis={analysis} trackId="demo" height={160} deck />
          </div>
        </Panel>
        <Panel className="mt-4">
          <PanelHeader title="Whole set" />
          <div className="p-4">
            <BenchTimeline />
          </div>
        </Panel>
        <Panel className="mt-4">
          <PanelHeader title="Library" />
          <div data-bench="table">
            <DemoTrackTable count={10000} className="h-80" />
          </div>
        </Panel>
      </PageBody>
    </Page>
  );
}
