// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { apiUrl } from "@/api/client";
import { CoverArt, spotifyImage } from "./CoverArt";

afterEach(cleanup);

const SPOTIFY = "https://i.scdn.co/image/ab67616d0000b273abcdef";

describe("spotifyImage", () => {
  it("picks the smallest variant that stays sharp at 2x", () => {
    expect(spotifyImage(SPOTIFY, 32)).toBe("https://i.scdn.co/image/ab67616d00004851abcdef");
    expect(spotifyImage(SPOTIFY, 120)).toBe("https://i.scdn.co/image/ab67616d00001e02abcdef");
    expect(spotifyImage(SPOTIFY, 200)).toBe(SPOTIFY);
  });

  it("leaves other URLs alone", () => {
    expect(spotifyImage("https://example.com/a.jpg", 32)).toBe("https://example.com/a.jpg");
  });
});

describe("CoverArt", () => {
  it("renders the image with alt text, resized for Spotify", () => {
    render(<CoverArt src={SPOTIFY} size={32} alt="Album" />);
    const img = screen.getByRole("img", { name: "Album" });
    expect(img.getAttribute("src")).toBe(spotifyImage(SPOTIFY, 32));
    expect(img.getAttribute("loading")).toBe("lazy");
  });

  it("routes embedded art through the API base", () => {
    render(<CoverArt src="/api/covers/x/1" alt="Embedded" />);
    expect(screen.getByRole("img", { name: "Embedded" }).getAttribute("src")).toBe(apiUrl("/api/covers/x/1"));
  });

  it("falls back to the disc tile without a src", () => {
    const { container } = render(<CoverArt src={null} alt="No cover" />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByLabelText("No cover")).toBeTruthy();
  });

  it("falls back to the disc tile when the image fails", () => {
    const { container } = render(<CoverArt src={SPOTIFY} alt="Broken" />);
    fireEvent.error(screen.getByRole("img", { name: "Broken" }));
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByLabelText("Broken")).toBeTruthy();
  });

  it("hides the loading placeholder once loaded", () => {
    const { container } = render(<CoverArt src={SPOTIFY} alt="Loads" />);
    expect(container.querySelector(".animate-pulse-soft")).not.toBeNull();
    fireEvent.load(screen.getByRole("img", { name: "Loads" }));
    expect(container.querySelector(".animate-pulse-soft")).toBeNull();
  });
});
