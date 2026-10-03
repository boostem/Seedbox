/** Search session memory (module scope): the last result list, selection and scroll, plus recent terms. */
import type { SearchPage } from "@/api/types";

export interface SearchMemo {
  term: string;
  pages: SearchPage[];
  selectedId: string | null;
  scrollTop: number;
}

let last: SearchMemo | null = null;

export const searchMemo = {
  get: () => last,
  set: (m: SearchMemo | null) => {
    last = m;
  },
  patch: (p: Partial<SearchMemo>) => {
    if (last) last = { ...last, ...p };
  },
};

const RECENT_KEY = "seedbox.search.recent";
const GROUP_KEY = "seedbox.search.group";

export function loadRecent(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string").slice(0, 8) : [];
  } catch {
    return [];
  }
}

export function pushRecent(term: string): string[] {
  const t = term.trim();
  const next = [t, ...loadRecent().filter((x) => x.toLowerCase() !== t.toLowerCase())].slice(0, 8);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* ignore */
  }
  return next;
}

export function clearRecent(): void {
  try {
    localStorage.removeItem(RECENT_KEY);
  } catch {
    /* ignore */
  }
}

export function loadGroupPref(): boolean {
  try {
    return localStorage.getItem(GROUP_KEY) !== "0";
  } catch {
    return true;
  }
}

export function saveGroupPref(v: boolean): void {
  try {
    localStorage.setItem(GROUP_KEY, v ? "1" : "0");
  } catch {
    /* ignore */
  }
}
