/**
 * Tiny hash router. Routes: #/search[?q=], #/crate/<seed_id>, #/downloads[/<job_id>], #/crates, #/set/<folder>,
 * #/setlists, #/setlist/<id>, #/settings, #/ui (design-system gallery). Path segments are URI-encoded (folder names contain spaces etc).
 */
import { type AnchorHTMLAttributes, type MouseEvent, useSyncExternalStore } from "react";

export type Route =
  | { name: "search" }
  | { name: "crate"; seedId: string }
  | { name: "downloads"; jobId: string }
  | { name: "crates" }
  | { name: "set"; folder: string }
  | { name: "setlists" }
  | { name: "setlist"; id: string }
  | { name: "settings" }
  | { name: "ui" }
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
  const [head, arg] = segs;
  switch (head) {
    case undefined:
    case "search":
      return { name: "search" };
    case "crate":
      return arg ? { name: "crate", seedId: arg } : { name: "search" };
    case "downloads":
      return { name: "downloads", jobId: arg ?? "" }; // "" = the job list
    case "crates":
      return { name: "crates" };
    case "set":
      return arg ? { name: "set", folder: segs.slice(1).join("/") } : { name: "crates" };
    case "setlists":
      return { name: "setlists" };
    case "setlist":
      return arg ? { name: "setlist", id: arg } : { name: "setlists" };
    case "settings":
      return { name: "settings" };
    case "ui":
      return { name: "ui" };
    default:
      return { name: "notFound", path: segs.join("/") };
  }
}

/** Route → "#/…" href. Optional query params are appended (`?q=…`). */
export function href(route: Route, query?: Record<string, string | number | null | undefined>): string {
  const e = encodeURIComponent;
  let path: string;
  switch (route.name) {
    case "crate":
      path = `crate/${e(route.seedId)}`;
      break;
    case "downloads":
      path = route.jobId ? `downloads/${e(route.jobId)}` : "downloads";
      break;
    case "set":
      path = `set/${e(route.folder)}`;
      break;
    case "setlist":
      path = `setlist/${e(route.id)}`;
      break;
    case "notFound":
      path = route.path;
      break;
    default:
      path = route.name;
  }
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
  const hash = useSyncExternalStore(subscribe, getHash);
  return parseHash(hash);
}

/** Query params of the current hash (#/search?q=fisher&page=2). */
export function useHashQuery(): URLSearchParams {
  const hash = useSyncExternalStore(subscribe, getHash);
  return splitHash(hash).query;
}

/** Replace the current hash's query params without adding a history entry (e.g. search term while typing). */
export function setHashQuery(params: Record<string, string | number | null | undefined>, opts = { replace: true }): void {
  const { segs } = splitHash(window.location.hash);
  const qs = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => [k, String(v)]),
  ).toString();
  const target = `#/${segs.map(encodeURIComponent).join("/")}${qs ? `?${qs}` : ""}`;
  navigate(target, opts);
}

type LinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { to: Route | string };

/** <a> for internal routes; keeps native behaviour (middle-click etc.). */
export function Link({ to, onClick, ...rest }: LinkProps) {
  const target = typeof to === "string" ? to : href(to);
  return (
    <a
      href={target}
      draggable={false}
      onClick={(e: MouseEvent<HTMLAnchorElement>) => {
        onClick?.(e);
      }}
      {...rest}
    />
  );
}
