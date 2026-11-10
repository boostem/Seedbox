/**
 * Low-power mode (#85): the latest GET /api/power, kept current by the `power` event. It sets the waveforms' frame cap
 * and the stretcher's profile, and tells the analysis queue what is paused; the top bar and Settings read it to say so.
 */
import { create } from "zustand";
import { api } from "@/api/client";
import { onServerEvent } from "@/api/events";
import type { PowerMode, PowerStatus } from "@/api/types";
import { setFrameCap } from "@/lib/frameCap";
import { setLowPower } from "@/audio/engine/power";
import { useApp } from "@/store/app";
import { useJobs } from "@/store/jobs";

export const usePower = create<{ status: PowerStatus | null }>()(() => ({ status: null }));

export function applyPower(status: PowerStatus): void {
  usePower.setState({ status });
  setFrameCap(status.limits.fps);
  setLowPower(status.low_power);
  const pausedPools = [...(status.limits.stems_paused ? ["heavy"] : []), ...(status.limits.analysis_paused ? ["light"] : [])];
  useJobs.setState((s) => ({ pausedPools, workers: { ...s.workers, light: status.limits.light_workers } }));
}

export function refreshPower(): void {
  void api.power.status({ quiet: true }).then(applyPower, () => {});
}

export function setPowerMode(mode: PowerMode): void {
  void api.power.setMode(mode).then(applyPower, () => {});
}

let started = false;
/** Follow the `power` event (once, from <App>); ask again after a reconnect, as a restarted engine says nothing. */
export function followPower(): () => void {
  if (started) return () => {};
  started = true;
  refreshPower();
  const off = onServerEvent((ev) => {
    if (ev.type === "power") applyPower(ev.data as PowerStatus);
  });
  const offConnection = useApp.subscribe((s, prev) => {
    if (s.connection === "live" && prev.connection !== "live") refreshPower();
  });
  return () => {
    started = false;
    off();
    offConnection();
  };
}
