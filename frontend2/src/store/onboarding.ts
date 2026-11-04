/**
 * The first-run flow (#80): whether it is open. The engine decides at launch (GET /api/onboarding); once open it stays
 * open until it is finished, even when a scan adds tracks meanwhile. When it can't tell, the app just opens.
 */
import { create } from "zustand";
import { api } from "@/api/client";
import { navigate, parseHash } from "@/lib/router";
import { useModels } from "@/store/models";

export const useOnboarding = create<{ open: boolean }>()(() => ({ open: false }));

export async function loadOnboarding(): Promise<void> {
  if (parseHash(location.hash).name === "bench") return; // `seedbox2.bench frontend` measures that page on a fresh engine
  try {
    const { first_run } = await api.onboarding.status({ quiet: true });
    if (first_run) useOnboarding.setState({ open: true });
  } catch {
    // the engine is down: the banner says so
  }
}

export function reopenOnboarding(): void {
  useOnboarding.setState({ open: true });
}

/** Finish (or skip) setup. Models nobody started stay undownloaded: "skip" holds on the next launch too, and
 * Settings → Models has them whenever they are wanted. */
export async function finishOnboarding(): Promise<void> {
  const models = useModels.getState().status?.models ?? [];
  const waiting = models.filter((m) => !m.installed);
  const skipped = waiting.length > 0 && waiting.every((m) => m.state === "missing" && m.done === 0);
  try {
    if (skipped) await api.models.settings({ auto_download: false }, { quiet: true });
    await api.onboarding.finish({ quiet: true });
  } catch {
    // it shows again next time: no reason to hold up the app
  }
  useOnboarding.setState({ open: false });
  navigate({ name: "library" });
}
