/** The waveform's Settings: mode (RGB / 3-band / stacked) and the GPU renderer, with a live preview. */
import { useMemo } from "react";
import { InfoRow, Panel, PanelHeader, Switch, Tabs } from "@/components/ui";
import { demoAnalysis } from "@/lib/demo/analysis";
import { webgl2Available } from "./gl/renderer";
import { STEM_VIEWS, useWaveSettings, WAVE_FOLLOWS, WAVE_MODES, waveSettings } from "./settings";
import { WaveOverview } from "./WaveOverview";

/** RGB / 3-band / stacked, as a segmented control. */
export function WaveModeTabs({ className }: { className?: string }) {
  const mode = useWaveSettings((s) => s.mode);
  return <Tabs variant="segmented" items={WAVE_MODES} value={mode} onChange={waveSettings.setMode} className={className} />;
}

/** Stacked / lanes, as a segmented control. */
export function StemViewTabs({ className }: { className?: string }) {
  const view = useWaveSettings((s) => s.stemView);
  return <Tabs variant="segmented" items={STEM_VIEWS} value={view} onChange={waveSettings.setStemView} className={className} />;
}

/** Centred / page, as a segmented control. */
export function WaveFollowTabs({ className }: { className?: string }) {
  const follow = useWaveSettings((s) => s.follow);
  return <Tabs variant="segmented" items={WAVE_FOLLOWS} value={follow} onChange={waveSettings.setFollow} className={className} />;
}

export function WaveSettingsPanel({ className }: { className?: string }) {
  const gpu = useWaveSettings((s) => s.gpu);
  const available = useMemo(webgl2Available, []);
  const preview = useMemo(demoAnalysis, []);
  return (
    <Panel className={className}>
      <PanelHeader title="Waveforms" description="How every waveform looks: the library, the track view, the transition editor and the decks" />
      <InfoRow label="Mode">
        <WaveModeTabs />
      </InfoRow>
      <InfoRow label="Stems">
        <StemViewTabs />
      </InfoRow>
      <InfoRow label="Renderer">
        <Switch checked={gpu && available} disabled={!available} onChange={waveSettings.setGpu} label="Draw on the GPU (WebGL2)" size="sm" />
        <span className="text-xs text-fg-subtle">{!available ? "WebGL2 isn't available here: Canvas 2D" : gpu ? "WebGL2" : "Canvas 2D"}</span>
      </InfoRow>
      <div className="px-4 py-3">
        <WaveOverview analysis={preview} height={44} sections />
      </div>
    </Panel>
  );
}
