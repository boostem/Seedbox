/** Banner on the track view when the analysed tempo doesn't match tunebat's: most likely the wrong song was downloaded. */
import { AlertTriangle, FolderOpen } from "lucide-react";
import { api } from "@/api/client";
import type { SetTrack, TrackAnalysis } from "@/api/types";
import { Button } from "@/components/ui";
import { fmtBpm } from "@/lib/format";
import { tempoMismatch } from "./model";

export function TempoWarning({ st, an }: { st: SetTrack; an: TrackAnalysis }) {
  if (!tempoMismatch(st.track.bpm, an)) return null;
  return (
    <div className="flex items-center gap-3 rounded-md border border-danger/40 bg-danger/[0.07] px-3 py-2">
      <AlertTriangle size={16} className="shrink-0 text-danger" />
      <div className="min-w-0 flex-1 text-sm">
        <div className="font-semibold text-danger">Probably a wrong download</div>
        <div className="text-fg-muted select-text">
          Tempo doesn't match tunebat: the audio runs at {an.bpm.toFixed(1)} BPM, tunebat says {fmtBpm(st.track.bpm)}. Check the file; if it's the wrong song, delete it and download another version.
        </div>
      </div>
      <Button size="sm" variant="outline" icon={FolderOpen} onClick={() => void api.reveal(st.path)}>
        Reveal in Finder
      </Button>
    </div>
  );
}
