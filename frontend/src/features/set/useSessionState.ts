import { useCallback, useState } from "react";

/** useState that survives remounts for the browser session (sessionStorage; silently in-memory if unavailable). */
export function useSessionState<T>(key: string, initial: T): [T, (v: T | ((p: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(key);
      if (raw != null) return JSON.parse(raw) as T;
    } catch {
      /* ignore */
    }
    return initial;
  });
  const set = useCallback(
    (v: T | ((p: T) => T)) =>
      setValue((prev) => {
        const next = typeof v === "function" ? (v as (p: T) => T)(prev) : v;
        try {
          sessionStorage.setItem(key, JSON.stringify(next));
        } catch {
          /* ignore */
        }
        return next;
      }),
    [key],
  );
  return [value, set];
}
