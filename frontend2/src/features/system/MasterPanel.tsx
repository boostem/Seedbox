/** Settings → Master (#58): the loudness target of the master chain, for playback and exports. */
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { MasterStatus, MasterTarget } from "@/api/types";
import { InfoRow, Panel, PanelHeader, Select } from "@/components/ui";

export function MasterPanel() {
  const [status, setStatus] = useState<MasterStatus | null>(null);
  useEffect(() => {
    void api.master.status({ quiet: true }).then(setStatus, () => {});
  }, []);
  return (
    <Panel className="max-w-3xl">
      <PanelHeader title="Master" description="How loud mixes play and export. Peaks stay under the limiter's ceiling" />
      {status && (
        <>
          <InfoRow label="Loudness target">
            <Select
              aria-label="Loudness target"
              value={status.target}
              onChange={(e) => void api.master.setTarget(e.target.value as MasterTarget).then(setStatus, () => {})}
            >
              <option value="streaming">Streaming, {status.targets.streaming} LUFS</option>
              <option value="club">Club, {status.targets.club} LUFS</option>
            </Select>
          </InfoRow>
          <InfoRow label="Ceiling">{status.ceiling_dbtp} dBTP</InfoRow>
        </>
      )}
    </Panel>
  );
}
