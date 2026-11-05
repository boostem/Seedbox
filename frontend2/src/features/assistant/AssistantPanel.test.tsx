// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistantStatus } from "@/api/types";

const assistant = vi.hoisted(() => ({ status: vi.fn(), update: vi.fn(), setKey: vi.fn(), deleteKey: vi.fn(), dryRun: vi.fn() }));
vi.mock("@/api/client", () => ({ api: { assistant } }));
const { AssistantPanel } = await import("./AssistantPanel");

const OFF: AssistantStatus = { enabled: false, model: "claude-sonnet-5-5", models: [{ id: "claude-sonnet-5-5", label: "Sonnet" }, { id: "claude-haiku-5-5", label: "Haiku" }], has_key: false, keychain: true };

beforeEach(() => {
  vi.resetAllMocks();
  assistant.status.mockResolvedValue(OFF);
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.open = true;
  };
});
afterEach(cleanup);

describe("AssistantPanel", () => {
  it("is off by default and turns on", async () => {
    assistant.update.mockResolvedValue({ ...OFF, enabled: true });
    render(<AssistantPanel />);
    const toggle = await screen.findByRole("switch");
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    await userEvent.click(toggle);
    await waitFor(() => expect(assistant.update).toHaveBeenCalledWith({ enabled: true }));
  });

  it("hands the key over, forgets it and never shows it", async () => {
    assistant.setKey.mockResolvedValue({ ...OFF, has_key: true });
    render(<AssistantPanel />);
    const input = (await screen.findByLabelText("Anthropic API key")) as HTMLInputElement;
    expect(input.type).toBe("password");
    await userEvent.type(input, "sk-ant-secret");
    await userEvent.click(screen.getByRole("button", { name: "Save in keychain" }));
    await waitFor(() => expect(assistant.setKey).toHaveBeenCalledWith("sk-ant-secret"));
    expect(await screen.findByText("Saved in this computer's keychain")).toBeTruthy();
    expect(document.body.innerHTML).not.toContain("sk-ant-secret");
  });

  it("says so when there's no keychain, rather than keeping the key elsewhere", async () => {
    assistant.status.mockResolvedValue({ ...OFF, keychain: false });
    render(<AssistantPanel />);
    expect(await screen.findByText(/no keychain/)).toBeTruthy();
    expect(screen.queryByLabelText("Anthropic API key")).toBeNull();
  });

  it("chooses a model", async () => {
    assistant.update.mockResolvedValue({ ...OFF, model: "claude-haiku-5-5" });
    render(<AssistantPanel />);
    await userEvent.selectOptions(await screen.findByLabelText("Assistant model"), "claude-haiku-5-5");
    await waitFor(() => expect(assistant.update).toHaveBeenCalledWith({ model: "claude-haiku-5-5" }));
  });

  it("states what is sent, and shows a dry run of the request", async () => {
    assistant.dryRun.mockResolvedValue({ url: "https://api.anthropic.com/v1/messages", headers: { authorization: "(your key, from the keychain)" }, body: { model: "claude-sonnet-5-5", messages: [] }, example_search_result: { matched: 1, tracks: [{ id: "t1", title: "Song" }] } });
    render(<AssistantPanel />);
    expect(await screen.findByText(/titles, artists, genre tags and our analysis numbers/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "See what would be sent" }));
    expect(await screen.findByText(/POST https:\/\/api.anthropic.com/)).toBeTruthy();
    expect(screen.getByText(/"title": "Song"/)).toBeTruthy();
    expect(assistant.dryRun).toHaveBeenCalledTimes(1);
  });
});
