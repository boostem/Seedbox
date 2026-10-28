// Placeholder for the set builder while the set loads: the running order's rows and transition cards, and an empty right pane.
import { Spinner } from "@/components/ui";

export function SetSkeleton() {
  return (
    <div className="flex min-h-0 flex-1" aria-busy aria-label="Loading set">
      <div className="flex w-[clamp(400px,40%,600px)] shrink-0 flex-col border-r border-line">
        <div className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3">
          <span className="label-caps">Running order</span>
          <span className="flex-1" />
          <Spinner size={11} className="text-fg-subtle" />
        </div>
        <div className="min-h-0 flex-1 overflow-hidden px-2 py-2">
          {Array.from({ length: 8 }, (_, i) => (
            <div key={i} style={{ opacity: 1 - i * 0.07 }}>
              <div className="flex h-11 items-center gap-2 pr-2 pl-1">
                <span className="w-4 shrink-0" />
                <span className="h-2.5 w-5 shrink-0 rounded-xs bg-raised/70" />
                <span className="size-8 shrink-0 animate-pulse-soft rounded-xs bg-raised" />
                <span className="flex min-w-0 flex-1 flex-col gap-1.5">
                  <span className="h-2.5 w-3/5 animate-pulse-soft rounded-xs bg-raised" />
                  <span className="h-2 w-2/5 rounded-xs bg-raised/60" />
                </span>
                <span className="h-[18px] w-8 shrink-0 rounded-xs bg-raised/70" />
                <span className="h-3 w-10 shrink-0 rounded-xs bg-raised" />
              </div>
              {i < 7 && (
                <div className="relative pl-[30px]">
                  <span className="absolute top-0 bottom-0 left-[21px] w-[2px] rounded-full bg-raised/60" />
                  <div className="my-0.5 flex h-7 items-center gap-1.5 px-2">
                    <span className="h-[18px] w-[30px] rounded-xs bg-raised/70" />
                    <span className="h-2.5 w-24 rounded-xs bg-raised/60" />
                    <span className="h-2 w-32 rounded-xs bg-raised/40" />
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
      <div className="flex min-w-0 flex-1 items-center justify-center gap-2 text-sm text-fg-subtle">
        <Spinner size={13} className="text-accent-hover" /> Loading set…
      </div>
    </div>
  );
}
