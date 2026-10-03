import { X } from "lucide-react";
import { type ReactNode, useEffect, useRef } from "react";
import { cn } from "@/lib/cn";
import { IconButton } from "./Button";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Right-aligned action buttons. */
  footer?: ReactNode;
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
  /** Close when clicking the backdrop (default true). */
  dismissable?: boolean;
}

const WIDTH = { sm: "w-[380px]", md: "w-[520px]", lg: "w-[720px]", xl: "w-[960px]" };

/** Modal dialog on native <dialog> (focus trap, Esc to close). Global hotkeys pause while it's open. */
export function Modal({ open, onClose, title, description, children, footer, size = "md", className, dismissable = true }: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  if (!open) return null;
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onMouseDown={(e) => {
        if (dismissable && e.target === ref.current) onClose();
      }}
      className={cn("m-auto max-h-[85vh] max-w-[92vw] overflow-hidden rounded-lg bg-overlay p-0 text-fg shadow-pop", WIDTH[size], className)}
    >
      <div className="flex max-h-[85vh] flex-col">
        {(title || description) && (
          <header className="flex items-start gap-3 border-b border-line px-4 py-3">
            <div className="min-w-0 flex-1">
              {title && <h2 className="text-md font-semibold">{title}</h2>}
              {description && <p className="mt-0.5 text-sm text-fg-muted">{description}</p>}
            </div>
            <IconButton icon={X} label="Close" size="sm" tooltip={false} onClick={onClose} />
          </header>
        )}
        <div className="min-h-0 flex-1 overflow-auto px-4 py-3 scroll-thin">{children}</div>
        {footer && <footer className="flex items-center justify-end gap-2 border-t border-line bg-surface/50 px-4 py-2.5">{footer}</footer>}
      </div>
    </dialog>
  );
}
