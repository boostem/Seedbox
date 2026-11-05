/**
 * Tiny hash router. Routes: #/ (home), #/library, #/crates, #/sets, #/discover, #/mixes, #/settings (#/system, its old
 * name, still works), #/ui (design tokens and the waveform), #/bench (the waveform alone, for
 * `python -m seedbox2.bench frontend`), #/listen (the blind listening test, not linked from anywhere), #/now-playing (?set=ID, the full-screen decks; ?demo=1 on
 * synthetic data).
 * Path segments are URI-encoded. Areas get their sub-routes as their tickets land.
 */
import { type AnchorHTMLAttributes, useSyncExternalStore } from "react";

export type Route =
  | { name: "home" }
  | { name: "library" }
  | { name: "crates" }
  | { name: "sets" }
  | { name: "discover" }
  | { name: "mixes" }
  | { name: "settings" }
  | { name: "ui" }
  | { name: "bench" }
  | { name: "listen" }
  | { name: "nowPlaying" }
  | { name: "notFound"; path: string };

export type RouteName = Route["name"];

function splitHash(hash: string): { segs: string[]; query: URLSearchParams } {
  const raw = hash.replace(/^#\/?/, "");
  const qi = raw.indexOf("?");
  const path = qi >= 0 ? raw.slice(0, qi) : raw;
  const query = new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : "");
  const segs = path.split("/").filter(Boolean).map((s) => {
    try {
      return decodeURIComponent(s);
    } catch {
      return s;
    }
  });
  return { segs, query };
}

export function parseHash(hash: string): Route {
  const { segs } = splitHash(hash);
  switch (segs[0]) {
    case undefined:
      return { name: "home" };
    case "library":
    case "crates":
    case "sets":
    case "discover":
    case "mixes":
    case "settings":
    case "ui":
    case "bench":
    case "listen":
      return { name: segs[0] };
    case "now-playing":
      return { name: "nowPlaying" };
    case "system":
      return { name: "settings" };
    default:
      return { name: "notFound", path: segs.join("/") };
  }
}

/** Route → "#/…" href. Optional query params are appended (`?q=…`). */
export function href(route: Route, query?: Record<string, string | number | null | undefined>): string {
  const path = route.name === "home" ? "" : route.name === "notFound" ? route.path : route.name === "nowPlaying" ? "now-playing" : route.name;
  const qs = query
    ? new URLSearchParams(
        Object.entries(query)
          .filter(([, v]) => v != null && v !== "")
          .map(([k, v]) => [k, String(v)]),
      ).toString()
    : "";
  return `#/${path}${qs ? `?${qs}` : ""}`;
}

export function navigate(to: Route | string, opts: { replace?: boolean } = {}): void {
  const target = typeof to === "string" ? to : href(to);
  if (opts.replace) {
    history.replaceState(null, "", target);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    window.location.hash = target;
  }
}

function subscribe(cb: () => void): () => void {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}
const getHash = () => window.location.hash;

/** The current route (re-renders on hash change). */
export function useRoute(): Route {
  return parseHash(useSyncExternalStore(subscribe, getHash));
}

/** Query params of the current hash (#/library?q=fisher). */
export function useHashQuery(): URLSearchParams {
  return splitHash(useSyncExternalStore(subscribe, getHash)).query;
}

type LinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { to: Route | string };

/** <a> for internal routes; keeps native behaviour (middle-click etc.). */
export function Link({ to, ...rest }: LinkProps) {
  return <a href={typeof to === "string" ? to : href(to)} draggable={false} {...rest} />;
}
