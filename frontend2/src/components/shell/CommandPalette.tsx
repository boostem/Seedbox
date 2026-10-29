import { CornerDownLeft, Search } from "lucide-react";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Kbd } from "@/components/ui";
import { cn } from "@/lib/cn";
import { paletteResults, type PaletteResult, useCommands } from "@/lib/commands";
import { usePresence } from "@/lib/usePresence";
import { ui, useUi } from "@/store/ui";

/** `text` with the characters at `indices` emphasised. */
function Highlight({ text, indices }: { text: string; indices: number[] }) {
  if (!indices.length) return <>{text}</>;
  const set = new Set(indices);
  const parts: { s: string; hit: boolean }[] = [];
  for (let i = 0; i < text.length; i++) {
    const hit = set.has(i);
    const last = parts[parts.length - 1];
    if (last && last.hit === hit) last.s += text[i];
    else parts.push({ s: text[i], hit });
  }
  return (
    <>
      {parts.map((p, i) => (p.hit ? <mark key={i} className="bg-transparent font-semibold text-fg">{p.s}</mark> : <Fragment key={i}>{p.s}</Fragment>))}
    </>
  );
}

function Row({ r, active, onHover, onRun, showGroup }: { r: PaletteResult; active: boolean; onHover: () => void; onRun: () => void; showGroup: boolean }) {
  const title = r.kind === "command" ? r.command.title : r.item.title;
  const Icon = r.kind === "command" ? r.command.icon : r.item.icon;
  return (
    <div
      role="option"
      aria-selected={active}
      data-active={active || undefined}
      onMouseMove={onHover}
      onClick={onRun}
      className={cn("flex h-9 cursor-default items-center gap-2.5 rounded-sm px-2.5 text-base", active ? "bg-selected text-fg" : "text-fg-muted")}
    >
      <span className="grid size-4 shrink-0 place-items-center text-fg-subtle">{Icon && <Icon size={15} />}</span>
      <span className="min-w-0 truncate">
        <Highlight text={title} indices={r.match.indices} />
      </span>
      {r.kind === "item" && r.item.subtitle && <span className="min-w-0 truncate text-sm text-fg-subtle">{r.item.subtitle}</span>}
      {r.kind === "command" && r.command.hint && <span className="shrink-0 rounded-xs bg-raised px-1 text-2xs text-fg-subtle">{r.command.hint}</span>}
      <span className="flex-1" />
      {showGroup && <span className="shrink-0 text-xs text-fg-faint">{r.group}</span>}
      {r.kind === "command" && r.command.combo && <Kbd combo={r.command.combo} subtle />}
      {active && <CornerDownLeft size={13} className="shrink-0 text-fg-subtle" aria-hidden />}
    </div>
  );
}

function Palette({ state, onAnimationEnd }: { state: "open" | "closed"; onAnimationEnd: ReturnType<typeof usePresence>["onAnimationEnd"] }) {
  const ref = useRef<HTMLDialogElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const commands = useCommands((s) => s.commands);
  const sources = useCommands((s) => s.sources);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const results = useMemo(() => paletteResults(query, commands, sources), [query, commands, sources]);
  const searching = query.trim() !== "";

  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  useLayoutEffect(() => {
    listRef.current?.querySelector("[data-active]")?.scrollIntoView({ block: "nearest" });
  }, [active, results]);

  const run = (r: PaletteResult | undefined) => {
    if (!r) return;
    ui.closePalette();
    if (r.kind === "command") r.command.run();
    else r.item.run();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const n = results.length;
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) {
      e.preventDefault();
      setActive((a) => (n ? (a + 1) % n : 0));
    } else if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) {
      e.preventDefault();
      setActive((a) => (n ? (a - 1 + n) % n : 0));
    } else if (e.key === "PageDown" || e.key === "PageUp") {
      e.preventDefault();
      setActive((a) => Math.min(n - 1, Math.max(0, a + (e.key === "PageDown" ? 8 : -8))));
    } else if (e.key === "Enter" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      run(results[active]);
    }
  };

  return (
    <dialog
      ref={ref}
      data-state={state}
      aria-label="Command palette"
      onAnimationEnd={onAnimationEnd}
      onCancel={(e) => {
        e.preventDefault();
        ui.closePalette();
      }}
      onMouseDown={(e) => {
        if (e.target === ref.current) ui.closePalette();
      }}
      className="mx-auto mt-[14vh] w-xl max-w-[92vw] overflow-hidden rounded-lg bg-overlay p-0 text-fg shadow-modal data-[state=closed]:animate-pop-out data-[state=open]:animate-pop-in"
    >
      <div className="flex h-12 items-center gap-2.5 border-b border-line px-3.5">
        <Search size={16} className="shrink-0 text-fg-subtle" />
        <input
          autoFocus
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="Go to a track, crate or set, or run a command…"
          aria-label="Search"
          aria-controls="palette-results"
          aria-activedescendant={results[active] ? `palette-${results[active].key}` : undefined}
          spellCheck={false}
          className="h-full min-w-0 flex-1 bg-transparent text-md text-fg outline-none placeholder:text-fg-faint"
        />
        <Kbd subtle>Esc</Kbd>
      </div>
      <div ref={listRef} id="palette-results" role="listbox" className="max-h-[52vh] overflow-y-auto p-1.5 scroll-thin">
        {results.length === 0 && (
          <div className="px-3 py-6 text-center text-sm text-fg-subtle">
            Nothing matches “{query.trim()}”.
            {sources.length === 0 && <div className="mt-1 text-fg-faint">Tracks, crates and sets show up here once the library lands.</div>}
          </div>
        )}
        {results.map((r, i) => (
          <Fragment key={r.key}>
            {!searching && r.group !== results[i - 1]?.group && <div className="px-2.5 pt-2 pb-1 label-caps">{r.group}</div>}
            <div id={`palette-${r.key}`}>
              <Row r={r} active={i === active} showGroup={searching} onHover={() => setActive(i)} onRun={() => run(r)} />
            </div>
          </Fragment>
        ))}
      </div>
      <div className="flex h-8 items-center gap-4 border-t border-line bg-surface/50 px-3.5 text-xs text-fg-subtle">
        <span className="flex items-center gap-1.5"><Kbd subtle>↑</Kbd><Kbd subtle>↓</Kbd> move</span>
        <span className="flex items-center gap-1.5"><Kbd subtle>↵</Kbd> run</span>
        <span className="flex-1" />
        <span className="flex items-center gap-1.5"><Kbd subtle>?</Kbd> all shortcuts</span>
      </div>
    </dialog>
  );
}

/** ⌘K: go to anything and run any command, fuzzy-matched. Pops in from above; leaves faster. */
export function CommandPalette() {
  const open = useUi((s) => s.paletteOpen);
  const p = usePresence(open);
  return p.mounted ? <Palette state={p.state} onAnimationEnd={p.onAnimationEnd} /> : null;
}
