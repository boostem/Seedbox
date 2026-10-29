import { CircleCheck, CircleMinus, CircleX, TriangleAlert, type LucideIcon } from "lucide-react";
import type { MixCheck, MixCheckStatus } from "@/api/types";
import { Button, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";

const STATUS: Record<MixCheckStatus, { icon: LucideIcon; cls: string; label: string }> = {
  pass: { icon: CircleCheck, cls: "text-ok", label: "OK" },
  warn: { icon: TriangleAlert, cls: "text-warn", label: "Check" },
  fail: { icon: CircleX, cls: "text-danger", label: "Problem" },
  na: { icon: CircleMinus, cls: "text-fg-faint", label: "Not measured" },
};

export interface MixChecksProps {
  checks: MixCheck[];
  /** Shows a Fix button on each warn/fail check (the editor wires it to the plan, #79). */
  onFix?: (check: MixCheck) => void;
  className?: string;
}

/** The objective checks of a rendered transition (bass overlap, vocal overlap, beat sync, loudness, key/tempo, low-end
 * masking) as a compact list: status icon, label, measured value, optional Fix. */
export function MixChecks({ checks, onFix, className }: MixChecksProps) {
  return (
    <ul className={cn("flex flex-col", className)}>
      {checks.map((c) => {
        const s = STATUS[c.status];
        return (
          <li key={c.id} className="flex min-h-8 items-center gap-2.5 border-b border-line/60 px-3 py-1 last:border-b-0">
            <Tooltip content={s.label}>
              <s.icon size={15} strokeWidth={2.25} className={s.cls} aria-label={s.label} />
            </Tooltip>
            <Tooltip content={c.detail} className="min-w-0 flex-1">
              <span className={cn("truncate text-sm", c.status === "na" ? "text-fg-subtle" : "text-fg")}>{c.label}</span>
            </Tooltip>
            <span className="num shrink-0 text-xs text-fg-muted">{c.value}</span>
            {onFix && (c.status === "warn" || c.status === "fail") && (
              <Button size="xs" variant="ghost" onClick={() => onFix(c)}>
                Fix
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
