import { AlertTriangle, RotateCw, X } from "lucide-react";
import { useState } from "react";
import { Button, IconButton } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useApp } from "@/store/app";
import { systemProblems } from "./problems";

/** A strip under the top bar for each thing that stops the app working, with its fix. Warnings can be dismissed
 * until the next launch; what breaks the app stays until it is fixed. */
export function SystemBanner() {
  const system = useApp((s) => s.system);
  const connection = useApp((s) => s.connection);
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set());
  const problems = systemProblems(system, connection).filter((p) => !dismissed.has(p.id));
  if (!problems.length) return null;
  return (
    <div className="shrink-0">
      {problems.map((p) => (
        <div key={p.id} role="alert" className={cn("flex items-center gap-2.5 border-b border-line px-5 py-2 text-sm", p.tone === "danger" ? "bg-danger/10" : "bg-warn/8")}>
          <AlertTriangle size={15} className={cn("shrink-0", p.tone === "danger" ? "text-danger" : "text-warn")} />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-fg">{p.title}.</span> <span className="select-text text-fg-muted">{p.fix}</span>
          </span>
          {p.recheck && (
            <Button size="sm" icon={RotateCw} onClick={() => void useApp.getState().refreshSystem()}>
              Check again
            </Button>
          )}
          {p.tone === "warn" && <IconButton icon={X} label="Dismiss" size="sm" onClick={() => setDismissed((d) => new Set(d).add(p.id))} />}
        </div>
      ))}
    </div>
  );
}
