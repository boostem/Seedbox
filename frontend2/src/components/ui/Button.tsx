import type { LucideIcon } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";
import { cn } from "@/lib/cn";
import { Kbd } from "./Kbd";
import { Spinner } from "./Spinner";
import { Tooltip } from "./Tooltip";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "outline";
export type ButtonSize = "xs" | "sm" | "md" | "lg";

const VARIANT: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover active:brightness-95 shadow-highlight",
  secondary: "bg-raised text-fg border border-line-strong hover:bg-overlay hover:border-fg-faint active:bg-raised",
  outline: "text-fg-muted border border-line-strong hover:text-fg hover:bg-raised",
  ghost: "text-fg-muted hover:text-fg hover:bg-raised active:bg-overlay",
  danger: "bg-danger/12 text-danger border border-danger/35 hover:bg-danger/22",
};

const SIZE: Record<ButtonSize, string> = {
  xs: "h-control-sm px-2 gap-1 text-xs rounded-sm",
  sm: "h-control-md px-2.5 gap-1.5 text-sm rounded-sm",
  md: "h-control-lg px-3 gap-2 text-base rounded-md",
  lg: "h-control-xl px-4 gap-2 text-md rounded-md",
};

const ICON_SIZE: Record<ButtonSize, number> = { xs: 12, sm: 14, md: 15, lg: 16 };

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: LucideIcon;
  iconRight?: LucideIcon;
  loading?: boolean;
  /** Shortcut hint shown at the right, e.g. "Mod+D". */
  kbd?: string;
  ref?: Ref<HTMLButtonElement>;
}

/** Text button. Primary = the one main action on a screen (e.g. Download); secondary for the rest. */
export function Button({ variant = "secondary", size = "sm", icon: Icon, iconRight: IconRight, loading, kbd, className, children, disabled, type = "button", ...rest }: ButtonProps) {
  const is = ICON_SIZE[size];
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={cn(
        "inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap transition-colors select-none",
        "disabled:pointer-events-none disabled:opacity-40",
        VARIANT[variant],
        SIZE[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner size={is} /> : Icon ? <Icon size={is} strokeWidth={2} className="shrink-0" /> : null}
      {children}
      {IconRight && <IconRight size={is} strokeWidth={2} className="shrink-0 opacity-80" />}
      {kbd && <Kbd className="ml-1" combo={kbd} subtle={variant !== "primary"} />}
    </button>
  );
}

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: LucideIcon;
  /** Accessible label; also the tooltip unless `tooltip={false}`. */
  label: string;
  tooltip?: ReactNode | false;
  variant?: Exclude<ButtonVariant, "outline"> | "outline";
  size?: ButtonSize;
  active?: boolean;
  loading?: boolean;
  ref?: Ref<HTMLButtonElement>;
}

const ICON_BTN: Record<ButtonSize, string> = {
  xs: "size-5 rounded-xs", // inside a dense row, below the control scale
  sm: "size-control-sm rounded-sm",
  md: "size-control-md rounded-sm",
  lg: "size-control-xl rounded-md",
};

/** Square icon-only button with tooltip. `active` = toggled on (e.g. grid/list view). */
export function IconButton({ icon: Icon, label, tooltip, variant = "ghost", size = "md", active, loading, className, type = "button", disabled, ...rest }: IconButtonProps) {
  const btn = (
    <button
      type={type}
      aria-label={label}
      aria-pressed={active}
      disabled={disabled || loading}
      className={cn(
        "inline-flex shrink-0 items-center justify-center transition-colors disabled:pointer-events-none disabled:opacity-40",
        variant === "ghost" ? "text-fg-muted hover:bg-raised hover:text-fg" : VARIANT[variant],
        active && "bg-overlay text-fg",
        ICON_BTN[size],
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner size={ICON_SIZE[size]} /> : <Icon size={ICON_SIZE[size]} strokeWidth={2} />}
    </button>
  );
  if (tooltip === false) return btn;
  return <Tooltip content={tooltip ?? label}>{btn}</Tooltip>;
}
