// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Button } from "./Button";
import { InfoRow, Page, PageBody, PageHeader, Panel, PanelHeader } from "./Panel";

afterEach(cleanup);

describe("Panel", () => {
  it("renders a section with a header and passes attributes", () => {
    render(
      <Panel aria-label="Tools">
        <PanelHeader title="Tools" description="External binaries" actions={<Button>Refresh</Button>} />
        <InfoRow label="yt-dlp">2026.09.12</InfoRow>
      </Panel>,
    );
    const panel = screen.getByRole("region", { name: "Tools" });
    expect(within(panel).getByRole("heading", { level: 3, name: "Tools" })).toBeTruthy();
    expect(within(panel).getByText("External binaries")).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "Refresh" })).toBeTruthy();
    expect(screen.getByText("yt-dlp").nextElementSibling?.textContent).toBe("2026.09.12");
  });
});

describe("PageHeader", () => {
  it("renders title, subtitle, leading, actions and toolbar", () => {
    render(
      <Page>
        <PageHeader title="Library" subtitle="1,204 tracks" leading={<span>logo</span>} actions={<Button>Scan</Button>}>
          <span>toolbar</span>
        </PageHeader>
        <PageBody>body</PageBody>
      </Page>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Library" })).toBeTruthy();
    for (const t of ["1,204 tracks", "logo", "toolbar", "body"]) expect(screen.getByText(t)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Scan" })).toBeTruthy();
  });

  it("omits optional parts", () => {
    render(<PageHeader title="Settings" />);
    expect(screen.getByRole("banner").textContent).toBe("Settings");
  });
});
