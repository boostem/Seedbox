/**
 * Keyboard way into the DAW: one lane (or one deck's loops or effects) at a time as a table of native inputs — beat,
 * value, curve / loop start, end, length / effect type, start, end, stutter repeat — with add, delete and the context
 * menu. Focusing a row selects it in the canvas;
 * a canvas selection switches the list to its lane. ↑/↓ in a number field step it (⇧ ×4, ⌥ fine), Enter commits.
 */
import { ChevronDown, ChevronRight, MoreHorizontal, Plus, Trash2 } from "lucide-react";
import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Curve, DeckFx, DeckFxType, DeckLoop, Keyframe, LaneId, MixPlan } from "@/api/types";
import { Button, IconButton, Select } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useSessionState } from "../useSessionState";
import { barLabel, CURVES, type DeckId, divLabel, FX_TYPES, getLane, laneRange, LANES, type LaneKey, type LaneMeta, laneUsed, laneValue, MAX_FX_BEATS, metaOf, setLane, STUTTER_DIV, STUTTER_DIVS } from "./lanes";

type Sel = Map<LaneKey, Set<number>>;
type LoopSel = { deck: DeckId; index: number } | null;
export type KeyMenu = { x: number; y: number; kind: "kf"; key: LaneKey; index: number } | { x: number; y: number; kind: "loop" | "fx"; deck: DeckId; index: number };
/** A lane, "loops.a" / "loops.b" or "fx.a" / "fx.b". */
type Target = LaneKey | `loops.${DeckId}` | `fx.${DeckId}`;

export interface KeyListProps {
  plan: MixPlan;
  sel: Sel;
  setSel: (s: Sel) => void;
  loopSel: LoopSel;
  setLoopSel: (s: LoopSel) => void;
  fxSel: LoopSel;
  setFxSel: (s: LoopSel) => void;
  update: (p: MixPlan) => void;
  menu: (m: KeyMenu) => void;
  /** The deck lanes offered (default: all) and whether the master tempo is. */
  lanes?: LaneMeta[];
  tempo?: boolean;
}

const LOOP_LENGTHS = [1, 2, 4, 8, 16];
const round = (v: number) => Math.round(v * 1000) / 1000;
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
const isLoops = (t: Target): t is `loops.${DeckId}` => t.startsWith("loops.");
const isFx = (t: Target): t is `fx.${DeckId}` => t.startsWith("fx.");

/** Number field that commits on Enter / blur (not per keystroke — a commit re-sorts the lane) and steps on ↑/↓. */
function NumField({ value, step, fine, min, max, label, onCommit, className, ...data }: { value: number; step: number; fine: number; min: number; max: number; label: string; onCommit: (v: number) => void; className?: string; "data-field"?: string }) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = (v: number) => {
    setDraft(null);
    if (Number.isFinite(v) && round(clamp(v, min, max)) !== value) onCommit(round(clamp(v, min, max)));
  };
  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      const s = e.altKey ? fine : e.shiftKey ? step * 4 : step;
      commit((draft != null ? Number(draft) : value) + (e.key === "ArrowUp" ? s : -s));
    } else if (e.key === "Enter") commit(Number(draft ?? value));
    else if (e.key === "Escape" && draft != null) {
      e.stopPropagation();
      setDraft(null);
    }
  };
  return (
    <input
      type="number"
      inputMode="decimal"
      aria-label={label}
      step={step}
      min={min}
      max={max}
      value={draft ?? String(value)}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft != null && commit(Number(draft))}
      onKeyDown={onKeyDown}
      className={cn("h-6 w-16 rounded-xs border border-line-strong bg-raised px-1.5 text-xs text-fg num outline-none hover:border-fg-faint focus:border-accent", className)}
      {...data}
    />
  );
}

export function KeyList({ plan, sel, setSel, loopSel, setLoopSel, fxSel, setFxSel, update, menu, lanes = LANES, tempo = true }: KeyListProps) {
  const [open, setOpen] = useSessionState("seedbox.mix.keyList", false);
  const keys = [...(tempo ? ["tempo"] : []), ...(["a", "b"] as DeckId[]).flatMap((d) => lanes.map((m) => `${d}.${m.id}`))] as LaneKey[];
  const firstUsed = keys.find((k) => laneUsed(k, getLane(plan, k))) ?? keys[0] ?? "a.volume";
  const [picked, setTarget] = useState<Target>(firstUsed);
  // a lane the current view doesn't offer (switched from manual) → the first one it does
  const target: Target = picked.startsWith("loops.") || picked.startsWith("fx.") || keys.includes(picked as LaneKey) ? picked : firstUsed;
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  const refocus = useRef<{ index: number; field: string } | null>(null);

  // a canvas selection (one lane, a loop or an effect) switches the list to it
  useEffect(() => {
    if (loopSel) return setTarget(`loops.${loopSel.deck}`);
    if (fxSel) return setTarget(`fx.${fxSel.deck}`);
    if (sel.size === 1) setTarget([...sel.keys()][0]);
  }, [sel, loopSel, fxSel]);

  // after a commit re-sorted the lane, follow the edited row
  useLayoutEffect(() => {
    const r = refocus.current;
    if (!r) return;
    refocus.current = null;
    bodyRef.current?.querySelector<HTMLElement>(`[data-row="${r.index}"] [data-field="${r.field}"]`)?.focus();
  });

  const lo = plan.window.start_beat - 32;
  const hi = plan.window.end_beat + 32;
  const selectKf = (key: LaneKey, i: number) => {
    const s = sel.get(key);
    if (loopSel) setLoopSel(null);
    if (fxSel) setFxSel(null);
    if (sel.size !== 1 || s?.size !== 1 || !s.has(i)) setSel(new Map([[key, new Set([i])]]));
  };
  const selectLoop = (deck: DeckId, i: number) => {
    if (sel.size) setSel(new Map());
    if (fxSel) setFxSel(null);
    if (loopSel?.deck !== deck || loopSel.index !== i) setLoopSel({ deck, index: i });
  };
  const selectFx = (deck: DeckId, i: number) => {
    if (sel.size) setSel(new Map());
    if (loopSel) setLoopSel(null);
    if (fxSel?.deck !== deck || fxSel.index !== i) setFxSel({ deck, index: i });
  };
  const menuAt = (e: { currentTarget: HTMLElement }, m: Omit<KeyMenu, "x" | "y">) => {
    const r = e.currentTarget.getBoundingClientRect();
    menu({ ...m, x: r.left, y: r.bottom + 2 } as KeyMenu);
  };
  // Shift+F10 / the context-menu key on a row opens the same menu as a right-click
  const rowKeys = (e: ReactKeyboardEvent<HTMLElement>, m: Omit<KeyMenu, "x" | "y">) => {
    if (e.key !== "ContextMenu" && !(e.key === "F10" && e.shiftKey)) return;
    e.preventDefault();
    menuAt({ currentTarget: e.target as HTMLElement }, m);
  };

  // ---- keyframes
  const lane = isLoops(target) || isFx(target) ? [] : getLane(plan, target);
  const editKf = (key: LaneKey, i: number, patch: Partial<Keyframe>, field: string) => {
    const kfs = getLane(plan, key);
    const kf = { ...kfs[i], ...patch };
    const next = kfs.map((k, j) => (j === i ? kf : k)).sort((a, b) => a.beat - b.beat);
    update(setLane(plan, key, next));
    const at = next.indexOf(kf);
    setSel(new Map([[key, new Set([at])]]));
    if (at !== i) refocus.current = { index: at, field };
  };
  const addKf = (key: LaneKey) => {
    const kfs = getLane(plan, key);
    const meta = metaOf(key);
    const from = sel.get(key)?.size ? Math.max(...sel.get(key)!) : kfs.length - 1;
    const beat = clamp(Math.round(((kfs[from]?.beat ?? plan.window.start_beat) + 4) * 4) / 4, lo, hi);
    const range = laneRange(key, plan);
    const value = round(clamp(kfs.length ? laneValue(kfs, beat) : meta.neutral, range[0], range[1]));
    const prev = [...kfs].reverse().find((k) => k.beat <= beat);
    const kf: Keyframe = { beat, value, curve: prev?.curve ?? "linear" };
    const next = [...kfs, kf].sort((a, b) => a.beat - b.beat);
    update(setLane(plan, key, next));
    setSel(new Map([[key, new Set([next.indexOf(kf)])]]));
    refocus.current = { index: next.indexOf(kf), field: "beat" };
  };
  const deleteKf = (key: LaneKey, i: number) => {
    const kfs = getLane(plan, key);
    if (kfs.length < 2) return;
    update(setLane(plan, key, kfs.filter((_, j) => j !== i)));
    setSel(new Map());
    refocus.current = { index: Math.min(i, kfs.length - 2), field: "beat" };
  };

  // ---- loops
  const deck = isLoops(target) ? (target.slice(6) as DeckId) : null;
  const loops = deck ? plan[deck].loops : [];
  const setLoops = (d: DeckId, next: DeckLoop[]) => update({ ...plan, [d]: { ...plan[d], loops: next } });
  const editLoop = (d: DeckId, i: number, patch: Partial<DeckLoop>, field: string) => {
    const l = { ...plan[d].loops[i], ...patch };
    const next = plan[d].loops.map((x, j) => (j === i ? l : x)).sort((a, b) => a.start_beat - b.start_beat);
    setLoops(d, next);
    const at = next.indexOf(l);
    setLoopSel({ deck: d, index: at });
    if (at !== i) refocus.current = { index: at, field };
  };
  const addLoop = (d: DeckId) => {
    const last = plan[d].loops.at(-1);
    const start = last ? last.end_beat : Math.round(plan.window.start_beat);
    const l: DeckLoop = { start_beat: start, end_beat: start + 4, source_beat: start, length_beats: 4 };
    const next = [...plan[d].loops, l].sort((a, b) => a.start_beat - b.start_beat);
    setLoops(d, next);
    setLoopSel({ deck: d, index: next.indexOf(l) });
    refocus.current = { index: next.indexOf(l), field: "start" };
  };
  const deleteLoop = (d: DeckId, i: number) => {
    setLoops(d, plan[d].loops.filter((_, j) => j !== i));
    setLoopSel(null);
    refocus.current = { index: Math.min(i, plan[d].loops.length - 2), field: "start" };
  };

  // ---- effects
  const fxDeck = isFx(target) ? (target.slice(3) as DeckId) : null;
  const fx = fxDeck ? (plan[fxDeck].fx ?? []) : [];
  const setFx = (d: DeckId, next: DeckFx[]) => update({ ...plan, [d]: { ...plan[d], fx: next } });
  /** An edit that keeps the effect between its neighbours and at most MAX_FX_BEATS long (else it is ignored). */
  const editFx = (d: DeckId, i: number, patch: Partial<DeckFx>, field: string) => {
    const all = plan[d].fx ?? [];
    const { div: _, ...rest } = { ...all[i], ...patch };
    const f: DeckFx = rest.type === "stutter" ? { ...rest, div: patch.div ?? all[i].div ?? STUTTER_DIV } : rest;
    const others = all.filter((_, j) => j !== i);
    if (f.end_beat - f.start_beat < 0.25 || f.end_beat - f.start_beat > MAX_FX_BEATS) return;
    if (others.some((o) => o.start_beat < f.end_beat && o.end_beat > f.start_beat)) return;
    const next = [...others, f].sort((a, b) => a.start_beat - b.start_beat);
    setFx(d, next);
    const at = next.indexOf(f);
    setFxSel({ deck: d, index: at });
    if (at !== i) refocus.current = { index: at, field };
  };
  const addFx = (d: DeckId) => {
    const last = (plan[d].fx ?? []).at(-1);
    const start = last ? last.end_beat : Math.round(plan.window.start_beat);
    const f: DeckFx = { type: "spinback", start_beat: start, end_beat: start + 1 };
    const next = [...(plan[d].fx ?? []), f];
    setFx(d, next);
    setFxSel({ deck: d, index: next.length - 1 });
    refocus.current = { index: next.length - 1, field: "type" };
  };
  const deleteFx = (d: DeckId, i: number) => {
    const all = plan[d].fx ?? [];
    setFx(d, all.filter((_, j) => j !== i));
    setFxSel(null);
    refocus.current = { index: Math.min(i, all.length - 2), field: "type" };
  };

  const meta = isLoops(target) || isFx(target) ? null : metaOf(target);
  const range = isLoops(target) || isFx(target) ? null : laneRange(target, plan);
  const cell = "px-1.5 py-0.5";
  const count = isLoops(target) ? loops.length : fxDeck ? fx.length : lane.length;

  return (
    <section className="mt-2 rounded-sm border border-line bg-surface">
      <div className="flex h-8 items-center gap-2 pr-1.5 pl-1">
        <button type="button" aria-expanded={open} aria-controls="mix-key-list" onClick={() => setOpen((v) => !v)} className="flex h-6 items-center gap-1 rounded-xs px-1.5 text-xs font-medium text-fg-muted hover:bg-raised hover:text-fg">
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          Keys &amp; loops
          <span className="text-2xs font-normal text-fg-subtle">— edit with the keyboard</span>
        </button>
        {open && (
          <>
            <span className="flex-1" />
            <Select aria-label="Lane" value={target} onChange={(e) => setTarget(e.target.value as Target)} className="h-6 text-xs">
              {tempo && <option value="tempo">Master tempo</option>}
              {(["a", "b"] as DeckId[]).map((d) => (
                <optgroup key={d} label={`Deck ${d.toUpperCase()}`}>
                  {lanes.map((m) => {
                    const k = `${d}.${m.id as LaneId}` as LaneKey;
                    return (
                      <option key={k} value={k}>
                        {d.toUpperCase()} · {m.label}
                        {laneUsed(k, getLane(plan, k)) ? "" : " (unused)"}
                      </option>
                    );
                  })}
                  <option value={`loops.${d}`}>
                    {d.toUpperCase()} · Loops ({plan[d].loops.length})
                  </option>
                  <option value={`fx.${d}`}>
                    {d.toUpperCase()} · Effects ({plan[d].fx?.length ?? 0})
                  </option>
                </optgroup>
              ))}
            </Select>
            <Button size="xs" variant="ghost" icon={Plus} onClick={() => (deck ? addLoop(deck) : fxDeck ? addFx(fxDeck) : addKf(target as LaneKey))}>
              {deck ? "Add loop" : fxDeck ? "Add effect" : "Add key"}
            </Button>
          </>
        )}
      </div>
      {open && (
        <div id="mix-key-list" className="max-h-56 overflow-y-auto border-t border-line/70 px-1 pb-1">
          {count === 0 ? (
            fxDeck ? (
              <p className="px-2 py-2 text-xs text-fg-subtle">No effects on deck {fxDeck.toUpperCase()} — “Add effect” puts a 1-beat spinback after the last.</p>
            ) : (
              <p className="px-2 py-2 text-xs text-fg-subtle">No loops on deck {deck?.toUpperCase()} — “Add loop” puts a 4-beat one after the last.</p>
            )
          ) : fxDeck ? (
            <table className="w-full text-xs">
              <thead className="text-left text-2xs text-fg-subtle">
                <tr>
                  <th className={cn(cell, "font-medium")}>Effect</th>
                  <th className={cn(cell, "font-medium")}>Start beat</th>
                  <th className={cn(cell, "font-medium")}>End beat</th>
                  <th className={cn(cell, "font-medium")}>Repeat every</th>
                  <th className={cell}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody ref={bodyRef}>
                {fx.map((f, i) => {
                  const on = fxSel?.deck === fxDeck && fxSel.index === i;
                  const m = { kind: "fx", deck: fxDeck, index: i } as const;
                  return (
                    <tr key={i} data-row={i} className={cn(on && "bg-accent/10")} onFocus={() => selectFx(fxDeck, i)} onKeyDown={(e) => rowKeys(e, m)}>
                      <td className={cell}>
                        <Select aria-label={`Effect ${i + 1} type`} value={f.type} onChange={(e) => editFx(fxDeck, i, { type: e.target.value as DeckFxType }, "type")} className="h-6 text-xs capitalize" data-field="type">
                          {FX_TYPES.map((t) => (
                            <option key={t} value={t}>
                              {t}
                            </option>
                          ))}
                        </Select>
                      </td>
                      <td className={cell}>
                        <span className="flex items-center gap-1.5">
                          <NumField data-field="start" label={`Effect ${i + 1} start beat`} value={f.start_beat} step={1} fine={0.25} min={lo} max={f.end_beat - 0.25} onCommit={(v) => editFx(fxDeck, i, { start_beat: v }, "start")} />
                          <span className="text-fg-subtle num">bar {barLabel(f.start_beat)}</span>
                        </span>
                      </td>
                      <td className={cell}>
                        <span className="flex items-center gap-1.5">
                          <NumField data-field="end" label={`Effect ${i + 1} end beat`} value={f.end_beat} step={1} fine={0.25} min={f.start_beat + 0.25} max={f.start_beat + MAX_FX_BEATS} onCommit={(v) => editFx(fxDeck, i, { end_beat: v }, "end")} />
                          <span className="text-fg-subtle num">bar {barLabel(f.end_beat)}</span>
                        </span>
                      </td>
                      <td className={cell}>
                        {f.type === "stutter" ? (
                          <Select aria-label={`Effect ${i + 1} repeat length`} value={f.div ?? STUTTER_DIV} onChange={(e) => editFx(fxDeck, i, { div: Number(e.target.value) }, "div")} className="h-6 text-xs" data-field="div">
                            {[...new Set([...STUTTER_DIVS, f.div ?? STUTTER_DIV])].sort((a, b) => b - a).map((n) => (
                              <option key={n} value={n}>
                                {divLabel(n)} beat
                              </option>
                            ))}
                          </Select>
                        ) : (
                          <span className="text-fg-subtle">—</span>
                        )}
                      </td>
                      <td className={cn(cell, "text-right whitespace-nowrap")}>
                        <IconButton icon={MoreHorizontal} label={`Effect ${i + 1} menu (Shift+F10)`} size="xs" onClick={(e) => menuAt(e, m)} />
                        <IconButton icon={Trash2} label={`Delete effect ${i + 1}`} size="xs" onClick={() => deleteFx(fxDeck, i)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-left text-2xs text-fg-subtle">
                <tr>
                  <th className={cn(cell, "font-medium")}>{deck ? "Start beat" : "Beat"}</th>
                  <th className={cn(cell, "font-medium")}>{deck ? "End beat" : meta?.label}</th>
                  <th className={cn(cell, "font-medium")}>{deck ? "Loop length" : "Curve to next"}</th>
                  <th className={cell}>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody ref={bodyRef}>
                {deck
                  ? loops.map((l, i) => {
                      const on = loopSel?.deck === deck && loopSel.index === i;
                      const m = { kind: "loop", deck, index: i } as const;
                      return (
                        <tr key={i} data-row={i} className={cn("rounded-xs", on && "bg-accent/10")} onFocus={() => selectLoop(deck, i)} onKeyDown={(e) => rowKeys(e, m)}>
                          <td className={cell}>
                            <span className="flex items-center gap-1.5">
                              <NumField data-field="start" label={`Loop ${i + 1} start beat`} value={l.start_beat} step={1} fine={0.25} min={lo} max={l.end_beat - 0.25} onCommit={(v) => editLoop(deck, i, { start_beat: v }, "start")} />
                              <span className="text-fg-subtle num">bar {barLabel(l.start_beat)}</span>
                            </span>
                          </td>
                          <td className={cell}>
                            <span className="flex items-center gap-1.5">
                              <NumField data-field="end" label={`Loop ${i + 1} end beat`} value={l.end_beat} step={1} fine={0.25} min={l.start_beat + 0.25} max={hi} onCommit={(v) => editLoop(deck, i, { end_beat: v }, "end")} />
                              <span className="text-fg-subtle num">bar {barLabel(l.end_beat)}</span>
                            </span>
                          </td>
                          <td className={cell}>
                            <Select aria-label={`Loop ${i + 1} length`} value={l.length_beats} onChange={(e) => editLoop(deck, i, { length_beats: Number(e.target.value) }, "length")} className="h-6 text-xs" data-field="length">
                              {[...new Set([...LOOP_LENGTHS, l.length_beats])].sort((a, b) => a - b).map((n) => (
                                <option key={n} value={n}>
                                  {n} beat{n === 1 ? "" : "s"}
                                </option>
                              ))}
                            </Select>
                          </td>
                          <td className={cn(cell, "text-right whitespace-nowrap")}>
                            <IconButton icon={MoreHorizontal} label={`Loop ${i + 1} menu (Shift+F10)`} size="xs" onClick={(e) => menuAt(e, m)} />
                            <IconButton icon={Trash2} label={`Delete loop ${i + 1}`} size="xs" onClick={() => deleteLoop(deck, i)} />
                          </td>
                        </tr>
                      );
                    })
                  : lane.map((kf, i) => {
                      const key = target as LaneKey;
                      const on = !!sel.get(key)?.has(i);
                      const m = { kind: "kf", key, index: i } as const;
                      return (
                        <tr key={i} data-row={i} className={cn(on && "bg-accent/10")} onFocus={() => selectKf(key, i)} onKeyDown={(e) => rowKeys(e, m)}>
                          <td className={cell}>
                            <span className="flex items-center gap-1.5">
                              <NumField data-field="beat" label={`Key ${i + 1} beat`} value={kf.beat} step={1} fine={0.25} min={lo} max={hi} onCommit={(v) => editKf(key, i, { beat: v }, "beat")} />
                              <span className="text-fg-subtle num">bar {barLabel(kf.beat)}</span>
                            </span>
                          </td>
                          <td className={cell}>
                            <span className="flex items-center gap-1.5">
                              <NumField data-field="value" label={`Key ${i + 1} ${meta!.label.toLowerCase()}`} value={kf.value} step={meta!.step} fine={meta!.fine} min={range![0]} max={range![1]} onCommit={(v) => editKf(key, i, { value: v }, "value")} />
                              <span className="text-fg-subtle num">{meta!.fmt(kf.value)}</span>
                            </span>
                          </td>
                          <td className={cell}>
                            <Select aria-label={`Key ${i + 1} curve`} value={kf.curve} onChange={(e) => editKf(key, i, { curve: e.target.value as Curve }, "curve")} className="h-6 text-xs capitalize" data-field="curve">
                              {CURVES.map((c) => (
                                <option key={c} value={c}>
                                  {c}
                                </option>
                              ))}
                            </Select>
                          </td>
                          <td className={cn(cell, "text-right whitespace-nowrap")}>
                            <IconButton icon={MoreHorizontal} label={`Key ${i + 1} menu (Shift+F10)`} size="xs" onClick={(e) => menuAt(e, m)} />
                            <IconButton icon={Trash2} label={lane.length < 2 ? "A lane keeps at least one key" : `Delete key ${i + 1}`} size="xs" disabled={lane.length < 2} onClick={() => deleteKf(key, i)} />
                          </td>
                        </tr>
                      );
                    })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  );
}
