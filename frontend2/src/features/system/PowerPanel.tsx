/** Settings → Low-power mode (#85): automatic, always on or off; how the machine is doing; what is held back now. */
import type { PowerMode } from "@/api/types";
import { InfoRow, Panel, PanelHeader, Select } from "@/components/ui";
import { useJobs } from "@/store/jobs";
import { setPowerMode, usePower } from "@/store/power";
import { powerEffects, powerSource, powerWhy } from "./power";

export function PowerPanel() {
  const status = usePower((s) => s.status);
  const waitingStems = useJobs((s) => Object.values(s.queued).filter((k) => k === "stems").length);
  const waitingAnalysis = useJobs((s) => Object.values(s.queued).filter((k) => k !== "stems" && k !== "render").length);
  return (
    <Panel className="max-w-3xl">
      <PanelHeader title="Low-power mode" description="Keeps a laptop cool and its battery full: background work holds back, mixing and playback don't" />
      {status && (
        <>
          <InfoRow label="Turn on">
            <Select aria-label="When low-power mode turns on" value={status.mode} onChange={(e) => setPowerMode(e.target.value as PowerMode)}>
              <option value="auto">Automatically, on battery or when it runs hot</option>
              <option value="on">Always</option>
              <option value="off">Never</option>
            </Select>
          </InfoRow>
          <InfoRow label="This computer">{powerSource(status)}</InfoRow>
          <InfoRow label="Now">{status.low_power ? `On: ${powerWhy(status).toLowerCase()}` : "Off: nothing is held back"}</InfoRow>
          {status.low_power && (
            <ul className="list-disc space-y-1 border-t border-line py-2.5 pl-8 pr-4 text-sm text-fg-muted">
              {powerEffects(status, waitingStems, waitingAnalysis).map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </Panel>
  );
}
