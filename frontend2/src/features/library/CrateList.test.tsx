// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CrateList } from "./CrateList";
import { TRACKS_MIME, UNREADY_ALL_MIME, UNREADY_SOME_MIME } from "./LibraryTable";

afterEach(cleanup);

const dt = (types: string[], hashes: string[] = ["a"]) => ({ types, getData: (t: string) => (t === TRACKS_MIME ? JSON.stringify(hashes) : ""), dropEffect: "none" });

function list(onDrop = vi.fn()) {
  render(<CrateList scope={{ kind: "all" }} onScope={() => {}} total={2} crates={[{ id: 1, name: "Peak", track_count: 0 }]} sets={[{ id: 2, name: "Friday", track_count: 0 }]} smartCrates={[]} onDeleteSmart={() => {}} onDrop={onDrop} />);
  return onDrop;
}

describe("CrateList and tracks that aren't ready (#100)", () => {
  it("shows a set won't take a drag of unready tracks, and a crate will", () => {
    list();
    const set = screen.getByRole("button", { name: /Friday/ });
    fireEvent.dragEnter(set, { dataTransfer: dt([TRACKS_MIME, UNREADY_ALL_MIME]) });
    expect(set.textContent).toContain("Not ready");
    const crate = screen.getByRole("button", { name: /Peak/ });
    fireEvent.dragEnter(crate, { dataTransfer: dt([TRACKS_MIME, UNREADY_ALL_MIME]) });
    expect(crate.textContent).not.toContain("Not ready");
  });

  it("says only the ready ones go into a set when some aren't", () => {
    list();
    const set = screen.getByRole("button", { name: /New set/ });
    fireEvent.dragEnter(set, { dataTransfer: dt([TRACKS_MIME, UNREADY_SOME_MIME]) });
    expect(set.textContent).toContain("Ready ones only");
  });

  it("still hands the drop to the page, which says why", () => {
    const onDrop = list();
    fireEvent.drop(screen.getByRole("button", { name: /Friday/ }), { dataTransfer: dt([TRACKS_MIME, UNREADY_ALL_MIME], ["a", "b"]) });
    expect(onDrop).toHaveBeenCalledWith("set", 2, ["a", "b"]);
  });
});
