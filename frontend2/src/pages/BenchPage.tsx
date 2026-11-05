import { useEffect, useMemo } from "react";
import { player, usePlayer } from "@/audio/player";
import { Page, PageBody, PageHeader, Panel, PanelHeader } from "@/components/ui";
import { TrackWaveform, waveRendererInfo } from "@/features/waveform";
import { demoAnalysis } from "@/lib/demo/analysis";
import { toneWav } from "@/lib/demo/tone";
import { DemoTrackTable } from "@/lib/demo/tracks";

/** `window.__seedboxBench`: what `python -m seedbox2.bench frontend` drives over the DevTools protocol. */
export interface BenchHooks {
  ready: true;
  /** Plays a quiet tone as long as the demo track, so the playhead moves and the waveform follows it. Resolves to
   * the player's status ("playing", or "error" when the browser can't play). */
  play: (startAt?: number) => Promise<string>;
  stop: () => void;
  /** What draws the waveforms: "webgl2 (<GPU>)" or "canvas2d". */
  gpu: () => string;
}

declare global {
  interface Window {
    __seedboxBench?: BenchHooks;
  }
}

/** #/bench: the waveform and a 10,000-row virtualised table, for the frontend benchmark (not in the sidebar). The
 * whole-set timeline (#75) and the stem lanes (#78) join this page when they land. */
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
      gpu: waveRendererInfo,
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
            <TrackWaveform analysis={analysis} trackId="demo" height={160} />
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
