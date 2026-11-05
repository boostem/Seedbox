import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { StemsStatus } from "@/api/types";
import { Input, InfoRow, Panel, PanelHeader, Select, Switch } from "@/components/ui";
import { fmtBytes } from "@/lib/format";

/** Settings → Stems (#44): what the stems cache holds, its size limit, the file format and the model. */
export function StemsPanel() {
  const [status, setStatus] = useState<StemsStatus | null>(null);
  const [limit, setLimit] = useState("");
  const show = (s: StemsStatus) => {
    setStatus(s);
    setLimit(String(s.limit_gb));
  };
  useEffect(() => void api.stems.status({ quiet: true }).then(show, () => {}), []);
  const change = (changes: Parameters<typeof api.stems.settings>[0]) => void api.stems.settings(changes).then(show);
  const commitLimit = () => {
    const gb = Number(limit);
    if (status && limit.trim() !== "" && Number.isFinite(gb) && gb >= 0 && gb !== status.limit_gb) change({ limit_gb: gb });
    else if (status) setLimit(String(status.limit_gb));
  };

  return (
    <Panel className="max-w-3xl">
      <PanelHeader
        title="Stems"
        description={
          status
            ? `Drums, bass, other and vocals of ${status.usage.tracks} ${status.usage.tracks === 1 ? "track" : "tracks"}: ${fmtBytes(status.usage.bytes)}. Over the limit, the stems used longest ago go first.`
            : "Drums, bass, other and vocals of each track, separated in the background and kept in the cache folder"
        }
      />
      {status && (
        <>
          <InfoRow label="Size limit">
            <Input
              aria-label="Stems size limit in GB"
              type="number"
              min={0}
              step={1}
              suffix="GB"
              wrapperClassName="w-28"
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
              onBlur={commitLimit}
              onKeyDown={(e) => e.key === "Enter" && commitLimit()}
            />
          </InfoRow>
          <InfoRow label="Format">
            <Select aria-label="Stems file format" value={status.format} onChange={(e) => change({ format: e.target.value as StemsStatus["format"] })}>
              <option value="flac">FLAC: lossless, about 72 MB per 4 minutes</option>
              <option value="opus">Opus 160k: about 19 MB per 4 minutes</option>
            </Select>
          </InfoRow>
          <div className="px-4 py-2.5">
            <Switch size="sm" checked={status.low_power} onChange={(v) => change({ low_power: v })} label="Always use the INT8 model, with less RAM and about the same quality, not only in low-power mode" />
          </div>
        </>
      )}
    </Panel>
  );
}
