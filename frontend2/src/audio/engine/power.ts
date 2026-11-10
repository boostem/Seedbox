/** Whether the stretcher should run its cheaper profile: `store/power` says, and every engine there is listens. Kept apart from the engine so the shell doesn't load it for this. */
let low = false;
const listeners = new Set<(on: boolean) => void>();

export function isLowPower(): boolean {
  return low;
}

export function setLowPower(on: boolean): void {
  if (on === low) return;
  low = on;
  for (const l of listeners) l(on);
}

export function onLowPower(listener: (on: boolean) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
