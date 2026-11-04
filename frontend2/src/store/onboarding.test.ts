import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/api/client";
import type { ModelInfo, ModelsStatus } from "@/api/types";
import { useModels } from "@/store/models";
import { finishOnboarding, loadOnboarding, useOnboarding } from "./onboarding";

vi.stubGlobal("window", { addEventListener: () => {}, location: { hash: "" } });
vi.stubGlobal("location", { hash: "" });

const withModels = (...models: Partial<ModelInfo>[]) => useModels.setState({ status: { models } as ModelsStatus });

function stubs() {
  const settings = vi.spyOn(api.models, "settings").mockResolvedValue({} as ModelsStatus);
  const finish = vi.spyOn(api.onboarding, "finish").mockResolvedValue({ first_run: false });
  return { settings, finish };
}

afterEach(() => {
  vi.restoreAllMocks();
  useOnboarding.setState({ open: false });
  useModels.setState({ status: null });
});

describe("onboarding", () => {
  it("opens on a first run only", async () => {
    vi.spyOn(api.onboarding, "status").mockResolvedValue({ first_run: false });
    await loadOnboarding();
    expect(useOnboarding.getState().open).toBe(false);
    vi.spyOn(api.onboarding, "status").mockResolvedValue({ first_run: true });
    await loadOnboarding();
    expect(useOnboarding.getState().open).toBe(true);
  });

  it("opens the app when the engine can't say", async () => {
    vi.spyOn(api.onboarding, "status").mockRejectedValue(new ApiError(0, "down"));
    await loadOnboarding();
    expect(useOnboarding.getState().open).toBe(false);
  });

  it("leaves the bench page alone: it is measured on a fresh engine", async () => {
    const status = vi.spyOn(api.onboarding, "status").mockResolvedValue({ first_run: true });
    location.hash = "#/bench";
    await loadOnboarding();
    location.hash = "";
    expect(status).not.toHaveBeenCalled();
    expect(useOnboarding.getState().open).toBe(false);
  });

  it("makes skipping the models last", async () => {
    const { settings, finish } = stubs();
    withModels({ installed: false, state: "missing", done: 0 });
    useOnboarding.setState({ open: true });
    await finishOnboarding();
    expect(settings).toHaveBeenCalledWith({ auto_download: false }, { quiet: true });
    expect(finish).toHaveBeenCalled();
    expect(useOnboarding.getState().open).toBe(false);
  });

  it.each([
    ["installed", { installed: true, state: "installed", done: 10 }],
    ["downloading", { installed: false, state: "downloading", done: 3 }],
    ["failed, to retry on the next start", { installed: false, state: "failed", done: 3 }],
    ["partly downloaded", { installed: false, state: "missing", done: 3 }],
  ] as [string, Partial<ModelInfo>][])("leaves downloads on when a model is %s", async (_name, m) => {
    const { settings, finish } = stubs();
    withModels(m);
    await finishOnboarding();
    expect(settings).not.toHaveBeenCalled();
    expect(finish).toHaveBeenCalled();
  });

  it("still opens the app when saving fails", async () => {
    stubs().finish.mockRejectedValue(new ApiError(0, "down"));
    useOnboarding.setState({ open: true });
    await finishOnboarding();
    expect(useOnboarding.getState().open).toBe(false);
  });
});
