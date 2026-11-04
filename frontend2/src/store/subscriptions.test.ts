/**
 * Narrow zustand subscriptions (PERFORMANCE.md): a component re-renders whenever its selector's result changes, so
 * every `useStore(...)` call must select the few fields it shows. Fails on
 *   - whole-store subscriptions: `useStore()` or `useStore((s) => s)`,
 *   - selectors that build a new object or array (`(s) => ({ a: s.a })`, a new value on every change and an endless
 *     re-render in zustand 5) unless wrapped in `useShallow`,
 *   - per-frame fields in a render: the player's `position` changes 25 times a second; draw playheads from
 *     `player.time()` in a requestAnimationFrame loop instead.
 * Reading a store outside React (`useStore.getState()`, `useStore.subscribe`) is fine and not checked.
 */
import { describe, expect, it } from "vitest";

const FILES = Object.entries(import.meta.glob(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], { query: "?raw", import: "default", eager: true }) as Record<string, string>);

/** Fields that change about every animation frame, per store: never select them in a component. */
const PER_FRAME: Record<string, string[]> = { usePlayer: ["position"] };

/** Code without comments, keeping line numbers. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " ")).replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

/** The zustand hooks a set of files defines: `export const useX = create...`. */
export function storeHooks(files: [string, string][]): string[] {
  const out = new Set<string>();
  for (const [, text] of files) for (const m of code(text).matchAll(/export const (use[A-Z]\w*)\s*=\s*create\b/g)) out.add(m[1]);
  return [...out].sort();
}

/** The argument text of the call whose "(" is at `open`, or null when unbalanced. */
function argsAt(src: string, open: number): string | null {
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "(") depth++;
    else if (src[i] === ")" && --depth === 0) return src.slice(open + 1, i);
  }
  return null;
}

export function checkSubscriptions(files: [string, string][], hooks: string[]): string[] {
  const problems: string[] = [];
  for (const [file, text] of files) {
    const src = code(text);
    for (const hook of hooks) {
      for (const m of src.matchAll(new RegExp(`(?<![\\w.])${hook}\\s*\\(`, "g"))) {
        const line = src.slice(0, m.index).split("\n").length;
        const where = `${file}:${line} ${hook}`;
        const args = argsAt(src, m.index + m[0].length - 1);
        if (args == null) continue;
        const sel = args.trim();
        if (!sel) {
          problems.push(`${where}(): subscribes to the whole store; select the fields you need`);
          continue;
        }
        if (/^\(?\s*(\w+)\s*(:[^)]*)?\)?\s*=>\s*\1\s*$/.test(sel)) {
          problems.push(`${where}((s) => s): subscribes to the whole store; select the fields you need`);
          continue;
        }
        if (/=>\s*(\(\s*\{|\[)/.test(sel) && !/^useShallow\s*\(/.test(sel)) {
          problems.push(`${where}: the selector returns a new object/array each time; select fields one by one or wrap it in useShallow`);
        }
        for (const field of PER_FRAME[hook] ?? []) {
          if (new RegExp(`\\.${field}\\b`).test(sel)) {
            problems.push(`${where}: selects ${field}, which changes every frame; read it outside React (see PERFORMANCE.md)`);
          }
        }
      }
    }
  }
  return problems;
}

describe("zustand subscriptions", () => {
  const hooks = storeHooks(FILES);

  it("finds the stores", () => {
    expect(hooks).toEqual(expect.arrayContaining(["useApp", "usePlayer", "useToasts"]));
  });

  it("every component selects narrowly", () => {
    expect(checkSubscriptions(FILES, hooks)).toEqual([]);
  });

  it("flags whole-store, new-object and per-frame subscriptions", () => {
    const bad: [string, string][] = [
      ["a.tsx", "const all = usePlayer();"],
      ["b.tsx", "const all = usePlayer((s) => s);\nconst st = useApp((state: AppState) => state);"],
      ["c.tsx", "const { a, b } = useApp((s) => ({ a: s.a, b: s.b }));"],
      ["d.tsx", "const pos = usePlayer((s) => s.position);"],
    ];
    const found = checkSubscriptions(bad, ["useApp", "usePlayer"]).map((p) => p.split(":")[0] + ":" + p.split(":")[1].split(" ")[0] + " " + (p.match(/whole store|new object|position/)?.[0] ?? p));
    expect(found).toEqual(["a.tsx:1 whole store", "b.tsx:2 whole store", "b.tsx:1 whole store", "c.tsx:1 new object", "d.tsx:1 position"]);
  });

  it("allows narrow selectors, useShallow and reads outside React", () => {
    const good: [string, string][] = [
      ["e.tsx", "const id = usePlayer((s) => s.currentId === id && s.status === 'playing');"],
      ["f.tsx", "const [a, b] = useApp(useShallow((s) => [s.a, s.b]));\nusePlayer.getState().position;\nusePlayer.subscribe(draw);"],
      ["g.tsx", "// usePlayer() in a comment\nconst playing = useIsPlaying(id);"],
    ];
    expect(checkSubscriptions(good, ["useApp", "usePlayer"])).toEqual([]);
  });
});

describe("failure fixes", () => {
  it("say what to do about a failed model download", async () => {
    const { modelFix } = await import("./models");
    expect(modelFix("[Errno 28] No space left on device")).toContain("disk is full");
    expect(modelFix("SHA-256 mismatch")).toContain("damaged");
    expect(modelFix("timed out")).toContain("Check the connection");
    expect(modelFix(null)).toContain("retry");
  });

  it("say what to do about a failed import", async () => {
    const { importFix } = await import("./imports");
    expect(importFix({ state: "failed", kind: "xml" })).toContain("Export it again");
    expect(importFix({ state: "failed", kind: "watch" })).toContain("Watch folders");
    expect(importFix({ state: "failed", kind: "files" })).toContain("drop them again");
  });
});
