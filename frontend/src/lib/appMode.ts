/**
 * Which app this page is. One frontend, two HTML entries that set <html data-app>: index.html is Seedbox (find songs,
 * search, preview, download, tag) and setbox.html is Setbox (set order, transitions, vocal stems, full mixes,
 * setlists). Both share the crates folder and the settings; each server serves its own page.
 */
export type AppId = "seedbox" | "setbox";

export const APP: AppId = typeof document !== "undefined" && document.documentElement.dataset.app === "setbox" ? "setbox" : "seedbox";
export const isSetbox = APP === "setbox";
export const isSeedbox = APP === "seedbox";
export const APP_NAME = isSetbox ? "Setbox" : "Seedbox";
export const OTHER_APP_NAME = isSetbox ? "Seedbox" : "Setbox";
