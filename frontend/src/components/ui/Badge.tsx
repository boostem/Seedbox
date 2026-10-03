import type { LucideIcon } from "lucide-react";
import type { HTMLAttributes, ReactNode } from "react";
import type { Move } from "@/api/types";
import { keyColor } from "@/lib/camelot";
import { cn } from "@/lib/cn";
import { MOVE_HELP, MOVE_LABEL, moveColor } from "@/lib/moves";
import { Tooltip } from "./Tooltip";

export type BadgeTone = "neutral" | "accent" | "ok" | "warn" | "danger" | "info" | "boost";

const TONE: Record<BadgeTone, { soft: string; solid: string; outline: string }> = {
  neutral: { soft: "bg-raised text-fg-muted", solid: "bg-fg-muted text-bg", outline: "border-line-strong text-fg-muted" },
  accent: { soft: "bg-accent/15 text-accent-hover", solid: "bg-accent text-accent-fg", outline: "border-accent/50 text-accent-hover" },
  ok: { soft: "bg-ok/12 text-ok", solid: "bg-ok text-bg", outline: "border-ok/45 text-ok" },
  warn: { soft: "bg-warn/12 text-warn", solid: "bg-warn text-bg", outline: "border-warn/45 text-warn" },
  danger: { soft: "bg-danger/12 text-danger", solid: "bg-danger text-white", outline: "border-danger/45 text-danger" },
  info: { soft: "bg-info/12 text-info", solid: "bg-info text-bg", outline: "border-info/45 text-info" },
  boost: { soft: "bg-move-boost/12 text-move-boost", solid: "bg-move-boost text-bg", outline: "border-move-boost/45 text-move-boost" },
};

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  variant?: "soft" | "solid" | "outline";
  size?: "xs" | "sm";
  icon?: LucideIcon;
}

/** Small status label: "downloaded", "exists", "NEW", counts… */
export function Badge({ tone = "neutral", variant = "soft", size = "sm", icon: Icon, className, children, ...rest }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-xs font-medium whitespace-nowrap num",
        size === "xs" ? "h-4 px-1 text-2xs" : "h-[18px] px-1.5 text-xs",
        variant === "outline" ? "border" : "",
        TONE[tone][variant],
        className,
      )}
      {...rest}
    >
      {Icon && <Icon size={size === "xs" ? 10 : 11} strokeWidth={2.25} />}
      {children}
    </span>
  );
}

export interface KeyBadgeProps {
  camelot: string | null | undefined;
  /** Musical key name ("G♯ minor") shown as tooltip. */
  keyName?: string;
  size?: "xs" | "sm" | "md";
  /** soft = tinted chip (default), solid = filled, text = coloured text only (dense tables). */
  variant?: "soft" | "solid" | "text";
  /** Colour by the Camelot move instead of the key's own colour (e.g. the move from the seed / previous track). */
  move?: Move;
  dim?: boolean;
  className?: string;
}

/** Camelot key chip coloured from the single Camelot colour map (lib/camelot.ts). */
export function KeyBadge({ camelot, keyName, size = "sm", variant = "soft", move, dim, className }: KeyBadgeProps) {
  const c = keyColor(camelot);
  const label = (camelot ?? "").trim().toUpperCase() || "?";
  const color = move ? moveColor(move) : c.base;
  const style =
    variant === "solid"
      ? { background: color, color: c.solidFg }
      : variant === "text"
        ? { color }
        : move
          ? { color, background: `color-mix(in srgb, ${color} 14%, transparent)`, boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${color} 32%, transparent)` }
          : { color, background: c.bg, boxShadow: `inset 0 0 0 1px ${c.border}` };
  const chip = (
    <span
      style={style}
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-xs font-semibold tracking-tight num",
        size === "xs" && "h-4 min-w-[26px] px-1 text-2xs",
        size === "sm" && "h-[18px] min-w-[32px] px-1 text-xs",
        size === "md" && "h-6 min-w-[40px] px-1.5 text-sm",
        variant === "text" && "min-w-0 px-0",
        dim && "opacity-55",
        className,
      )}
    >
      {label}
    </span>
  );
  return keyName ? <Tooltip content={`${keyName} · ${label}`}>{chip}</Tooltip> : chip;
}

export interface ScoreChipProps {
  value: number;
  move?: Move;
  /** false = failed the pre-tick rule → outlined + dimmed. */
  passed?: boolean;
  size?: "sm" | "md";
  className?: string;
}

/** 0-100 compatibility score coloured by move; failing tracks are dimmed/outlined. */
export function ScoreChip({ value, move = "?", passed = true, size = "sm", className }: ScoreChipProps) {
  const color = moveColor(move);
  return (
    <span
      style={
        passed
          ? { color, background: `color-mix(in srgb, ${color} 15%, transparent)` }
          : { color: `color-mix(in srgb, ${color} 75%, var(--color-fg-subtle))`, boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${color} 30%, transparent)` }
      }
      className={cn(
        "relative inline-flex shrink-0 items-center justify-center overflow-hidden rounded-xs font-semibold num",
        size === "sm" ? "h-[18px] w-[30px] text-xs" : "h-6 w-9 text-sm",
        !passed && "opacity-75",
        className,
      )}
    >
      {Math.round(value)}
      {passed && <span className="absolute bottom-0 left-0 h-px" style={{ width: `${Math.max(0, Math.min(100, value))}%`, background: color }} />}
    </span>
  );
}

/** Camelot move label with its colour dot ("Relative", "+1", "Clash"…). */
export function MoveChip({ move, size = "sm", showHelp = true, className }: { move: Move; size?: "xs" | "sm"; showHelp?: boolean; className?: string }) {
  const color = moveColor(move);
  const chip = (
    <span
      className={cn("inline-flex shrink-0 items-center gap-1 rounded-xs font-medium whitespace-nowrap", size === "xs" ? "h-4 px-1 text-2xs" : "h-[18px] px-1.5 text-xs", className)}
      style={{ color, background: `color-mix(in srgb, ${color} 12%, transparent)` }}
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} />
      {MOVE_LABEL[move] ?? move}
    </span>
  );
  return showHelp ? <Tooltip content={MOVE_HELP[move]}>{chip}</Tooltip> : chip;
}

/** Plain reason chips (Score.chips): "−1.5% pitch", "energy +0.07". */
export function ReasonChips({ chips, max = 4, className }: { chips: string[]; max?: number; className?: string }) {
  const shown = chips.slice(0, max);
  const more = chips.length - shown.length;
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1 overflow-hidden", className)}>
      {shown.map((c) => (
        <span key={c} className="h-4 shrink-0 rounded-xs bg-raised px-1 text-2xs leading-4 whitespace-nowrap text-fg-muted num">
          {c}
        </span>
      ))}
      {more > 0 && (
        <Tooltip content={chips.slice(max).join(" · ")}>
          <span className="text-2xs text-fg-subtle">+{more}</span>
        </Tooltip>
      )}
    </span>
  );
}

/** Coloured dot + label for live status (connection, job state). */
export function StatusDot({ tone = "neutral", pulse, label, className }: { tone?: BadgeTone; pulse?: boolean; label?: ReactNode; className?: string }) {
  const bg: Record<BadgeTone, string> = {
    neutral: "bg-fg-subtle",
    accent: "bg-accent",
    ok: "bg-ok",
    warn: "bg-warn",
    danger: "bg-danger",
    info: "bg-info",
    boost: "bg-move-boost",
  };
  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      <span className={cn("size-[7px] shrink-0 rounded-full", bg[tone], pulse && "animate-pulse-soft")} />
      {label}
    </span>
  );
}
