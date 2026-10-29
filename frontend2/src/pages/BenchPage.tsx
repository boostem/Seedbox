import { useEffect, useMemo } from "react";
import { player, usePlayer } from "@/audio/player";
import { Page, PageBody, PageHeader, Panel, PanelHeader } from "@/components/ui";
import { TrackWaveform } from "@/features/waveform";
import { demoAnalysis } from "@/lib/demo/analysis";
import { DemoTrackTable } from "@/lib/demo/tracks";

/** `window.__seedboxBench`: what `python -m seedbox2.bench frontend` drives over the DevTools protocol. */
export interface BenchHooks {
  ready: true;
  /** Plays a quiet tone as long as the demo track, so the playhead moves and the waveform follows it. Resolves to
   * the player's status ("playing", or "error" when the browser can't play). */
  play: (startAt?: number) => Promise<string>;
  stop: () => void;
}

declare global {
  interface Window {
    __seedboxBench?: BenchHooks;
  }
}

/** A mono 16-bit WAV of a quiet 110 Hz tone: real playback without a backend or an audio file. */
function toneWav(seconds: number, sr = 8000): Blob {
  const n = Math.ceil(seconds * sr);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (at: number, s: string) => [...s].forEach((c, i) => v.setUint8(at + i, c.charCodeAt(0)));
  str(0, "RIFF");
  v.setUint32(4, 36 + n * 2, true);
  str(8, "WAVEfmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, sr, true);
  v.setUint32(28, sr * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, "data");
  v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.round(800 * Math.sin((2 * Math.PI * 110 * i) / sr)), true);
  return new Blob([buf], { type: "audio/wav" });
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
