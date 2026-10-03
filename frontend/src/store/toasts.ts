/** Toast notifications. Call `toast.error("…")` from anywhere (components, API client, audio service). */
import { create } from "zustand";

export type ToastLevel = "info" | "success" | "warning" | "error";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface Toast {
  id: number;
  level: ToastLevel;
  message: string;
  title?: string;
  action?: ToastAction;
  /** Toasts with the same key replace each other (e.g. "network") instead of stacking. */
  key?: string;
  /** ms until auto-dismiss; null = sticky. */
  timeout: number | null;
}

export interface ToastOptions {
  title?: string;
  action?: ToastAction;
  key?: string;
  timeout?: number | null;
}

interface ToastState {
  toasts: Toast[];
  push: (level: ToastLevel, message: string, opts?: ToastOptions) => number;
  dismiss: (id: number) => void;
  clear: () => void;
}

const DEFAULT_TIMEOUT: Record<ToastLevel, number> = { info: 4000, success: 3500, warning: 6000, error: 8000 };
const MAX_TOASTS = 5;
let nextId = 1;
const timers = new Map<number, ReturnType<typeof setTimeout>>();

export const useToasts = create<ToastState>()((set, get) => ({
  toasts: [],
  push: (level, message, opts = {}) => {
    const id = nextId++;
    const timeout = opts.timeout === undefined ? DEFAULT_TIMEOUT[level] : opts.timeout;
    const toast: Toast = { id, level, message, title: opts.title, action: opts.action, key: opts.key, timeout };
    set((s) => {
      const rest = opts.key ? s.toasts.filter((t) => t.key !== opts.key) : s.toasts;
      for (const t of s.toasts) if (!rest.includes(t)) clearTimer(t.id);
      return { toasts: [...rest, toast].slice(-MAX_TOASTS) };
    });
    if (timeout != null) timers.set(id, setTimeout(() => get().dismiss(id), timeout));
    return id;
  },
  dismiss: (id) => {
    clearTimer(id);
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
  },
  clear: () => {
    for (const id of timers.keys()) clearTimer(id);
    set({ toasts: [] });
  },
}));

function clearTimer(id: number) {
  const t = timers.get(id);
  if (t) clearTimeout(t);
  timers.delete(id);
}

export const toast = {
  info: (message: string, opts?: ToastOptions) => useToasts.getState().push("info", message, opts),
  success: (message: string, opts?: ToastOptions) => useToasts.getState().push("success", message, opts),
  warning: (message: string, opts?: ToastOptions) => useToasts.getState().push("warning", message, opts),
  error: (message: string, opts?: ToastOptions) => useToasts.getState().push("error", message, opts),
  dismiss: (id: number) => useToasts.getState().dismiss(id),
};
