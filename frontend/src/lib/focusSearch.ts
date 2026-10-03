/**
 * ⌘K "focus search" protocol (the shortcut itself lives in GlobalHotkeys). From any page `focusSearch()` navigates
 * to #/search and asks the search page to focus + select its box: the page calls `takePendingFocus()` on mount and
 * listens for FOCUS_SEARCH_EVENT while mounted.
 */
import { navigate, parseHash } from "./router";

export const FOCUS_SEARCH_EVENT = "seedbox:focus-search";

let pendingFocus = false;

/** Called by the search page on mount: was it opened by ⌘K (focus + select the text)? */
export function takePendingFocus(): boolean {
  const p = pendingFocus;
  pendingFocus = false;
  return p;
}

export function focusSearch(): void {
  if (parseHash(window.location.hash).name === "search") {
    window.dispatchEvent(new Event(FOCUS_SEARCH_EVENT));
  } else {
    pendingFocus = true;
    navigate({ name: "search" });
  }
}
