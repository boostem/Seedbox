/** In the top bar while low-power mode is on (#85): what is paused and why, in words. Click for the setting. */
import { BatteryMedium, Leaf, Thermometer } from "lucide-react";
import { Tooltip } from "@/components/ui";
import { navigate } from "@/lib/router";
import { useJobs } from "@/store/jobs";
import { usePower } from "@/store/power";
import { powerEffects, powerLabel, powerWhy } from "./power";

const ICONS = { battery: BatteryMedium, thermal: Thermometer, manual: Leaf };

export function PowerIndicator() {
  const status = usePower((s) => s.status);
  const waitingStems = useJobs((s) => Object.values(s.queued).filter((k) => k === "stems").length);
  const waitingAnalysis = useJobs((s) => Object.values(s.queued).filter((k) => k !== "stems" && k !== "render").length);
  if (!status?.low_power || !status.reason) return null;
  const Icon = ICONS[status.reason];
  return (
    <Tooltip
      side="bottom"
      content={
        <div className="max-w-80 space-y-1 text-left">
          <div className="font-medium">Low-power mode: {powerWhy(status).toLowerCase()}</div>
          {powerEffects(status, waitingStems, waitingAnalysis).map((e) => (
            <div key={e}>{e}</div>
          ))}
        </div>
      }
    >
      <button
        type="button"
        onClick={() => navigate({ name: "settings" })}
        className="flex h-7 items-center gap-1.5 rounded-sm px-2 text-xs text-warn transition-colors hover:bg-raised"
      >
        <Icon size={14} />
        <span className="whitespace-nowrap">{powerLabel(status)}</span>
      </button>
    </Tooltip>
  );
}
