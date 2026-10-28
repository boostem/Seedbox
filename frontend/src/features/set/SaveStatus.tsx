/** The set builder header's note on the last running-order save (nothing while idle). */
import { AlertTriangle, Check } from "lucide-react";
import { Spinner } from "@/components/ui";
import type { SaveState } from "./useSetModel";

export function SaveStatus({ save }: { save: SaveState }) {
  if (save === "saving") {
    return (
      <span className="inline-flex items-center gap-1 text-fg-subtle">
        <Spinner size={11} /> Saving order…
      </span>
    );
  }
  if (save === "saved") {
    return (
      <span className="inline-flex items-center gap-1 text-ok">
        <Check size={12} /> Order saved · TRCK, .m3u8, .xml updated
      </span>
    );
  }
  if (save === "reverted") {
    return (
      <span className="inline-flex items-center gap-1 text-warn">
        <AlertTriangle size={12} /> Couldn't save · move undone
      </span>
    );
  }
  if (save === "autoFailed") {
    return (
      <span className="inline-flex items-center gap-1 text-warn">
        <AlertTriangle size={12} /> Auto-order failed, order unchanged
      </span>
    );
  }
  return null;
}
