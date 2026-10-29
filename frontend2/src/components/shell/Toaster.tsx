import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { type ToastLevel, useToasts } from "@/store/toasts";

const ICON = { info: Info, success: CircleCheck, warning: TriangleAlert, error: CircleAlert } as const;
const COLOR: Record<ToastLevel, string> = { info: "text-info", success: "text-ok", warning: "text-warn", error: "text-danger" };
const BAR: Record<ToastLevel, string> = { info: "bg-info", success: "bg-ok", warning: "bg-warn", error: "bg-danger" };

/** Toast stack (bottom-right, above the mini player). Use `toast.*()` from @/store/toasts to show one. */
export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  const dismiss = useToasts((s) => s.dismiss);
  return (
    <div className="pointer-events-none fixed right-3 bottom-[68px] z-[900] flex w-[360px] flex-col gap-2" aria-live="polite">
      {toasts.map((t) => {
        const Icon = ICON[t.level];
        return (
          <div key={t.id} role={t.level === "error" ? "alert" : "status"} className="pointer-events-auto relative flex animate-toast-in gap-2.5 overflow-hidden rounded-md bg-overlay py-2.5 pr-2 pl-3 shadow-pop">
            <span className={cn("absolute inset-y-0 left-0 w-[3px]", BAR[t.level])} />
            <Icon size={16} className={cn("mt-px shrink-0", COLOR[t.level])} />
            <div className="min-w-0 flex-1 select-text">
              {t.title && <div className="truncate text-sm font-semibold">{t.title}</div>}
              <div className={cn("text-sm break-words", t.title ? "text-fg-muted" : "text-fg")}>{t.message}</div>
              {t.action && (
                <button
                  type="button"
                  className="mt-1.5 text-sm font-medium text-accent-hover hover:underline"
                  onClick={() => {
                    t.action?.onClick();
                    dismiss(t.id);
                  }}
                >
                  {t.action.label}
                </button>
              )}
            </div>
            <button type="button" aria-label="Dismiss" onClick={() => dismiss(t.id)} className="grid size-5 shrink-0 place-items-center rounded-xs text-fg-subtle hover:bg-raised hover:text-fg">
              <X size={13} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
