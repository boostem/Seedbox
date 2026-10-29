import type { LucideIcon } from "lucide-react";
import type { InputHTMLAttributes, ReactNode, Ref, SelectHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  icon?: LucideIcon;
  /** Text/element shown inside the right edge ("s", "MB", a clear button). */
  suffix?: ReactNode;
  size?: "sm" | "md" | "lg";
  invalid?: boolean;
  ref?: Ref<HTMLInputElement>;
  wrapperClassName?: string;
}

/** Text/number input. `size="lg"` for the big search box. */
export function Input({ icon: Icon, suffix, size = "sm", invalid, className, wrapperClassName, ref, ...rest }: InputProps) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-sm border bg-raised text-fg transition-colors focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/25",
        invalid ? "border-danger/70" : "border-line-strong hover:border-fg-faint",
        size === "sm" && "h-control-md px-2 text-sm",
        size === "md" && "h-control-lg px-2.5 text-base",
        size === "lg" && "h-control-xl rounded-md px-3 text-md",
        wrapperClassName,
      )}
    >
      {Icon && <Icon size={size === "lg" ? 17 : 14} className="shrink-0 text-fg-subtle" />}
      <input ref={ref} className={cn("min-w-0 flex-1 bg-transparent outline-none placeholder:text-fg-faint focus-visible:outline-none", className)} {...rest} />
      {suffix && <span className="shrink-0 text-xs text-fg-subtle">{suffix}</span>}
    </div>
  );
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn("h-control-md rounded-sm border border-line-strong bg-raised px-2 text-sm text-fg outline-none hover:border-fg-faint focus:border-accent", className)}
      {...rest}
    >
      {children}
    </select>
  );
}

export interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  /** Right side of the label row (current value, reset link). */
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
  htmlFor?: string;
}

/** Form row: label (+ aside), control, hint/error. */
export function Field({ label, hint, error, aside, children, className, htmlFor }: FieldProps) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={htmlFor} className="text-sm font-medium text-fg">
          {label}
        </label>
        {aside && <span className="text-xs text-fg-muted num">{aside}</span>}
      </div>
      {children}
      {error ? <p className="text-xs text-danger">{error}</p> : hint ? <p className="text-xs text-fg-subtle">{hint}</p> : null}
    </div>
  );
}
