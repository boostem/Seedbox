/**
 * "Play this transition" requests from outside the mix view (transition cards, page-level Space): stored until the
 * mix view for that pair is mounted and ready, then consumed once.
 */
import { useEffect } from "react";

let pending: string | null = null;
const listeners = new Set<() => void>();

export function requestPlay(key: string): void {
  pending = key;
  for (const l of listeners) l();
}

export function takePendingPlay(key: string): boolean {
  if (pending !== key) return false;
  pending = null;
  return true;
}

/** Runs `fn` when a play request for `key` arrives (or is already waiting) while `ready`. */
export function usePlayRequests(key: string, ready: boolean, fn: () => void): void {
  useEffect(() => {
    if (!ready) return;
    const check = () => {
      if (takePendingPlay(key)) fn();
    };
    check();
    listeners.add(check);
    return () => void listeners.delete(check);
  }, [key, ready, fn]);
}
