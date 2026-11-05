/**
 * The transition editor (#79): a saved plan on the live engine, at three levels. Simple writes the whole plan from a style
 * and a few amounts, Advanced has a curve per stem on that stem's waveform, Manual has every lane and keyframe. Whichever
 * it was, an edit is heard at once, is one step of undo, is saved a moment later and shows in the checks.
 *
 * Nothing here waits for a render.
 */
import { Pause, Play, Redo2, Repeat, RotateCcw, Undo2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type RefObject } from "react";
import { api, apiUrl } from "@/api/client";
import { LANES, STEM_LANES } from "@/api/mixplan.generated";
import type { Keyframe, MixPlan } from "@/api/mixplan.generated";
import type { EngineState, LiveEngine } from "@/audio/engine";
import { player } from "@/audio/player";
import { Button, Field, IconButton, Input, Select, Spinner, Tabs } from "@/components/ui";
import { wavePalette } from "@/features/waveform/palette";
import type { WaveData } from "@/features/waveform/prepare";
import { type StemWave } from "@/features/waveform/stems";
import { loadWave, useStemWave } from "@/features/waveform/waveData";
import { type CheckInput, fixCheck, planChecks } from "./checks";
import { CURVES, type DeckId, type LaneName, laneOf, moveKeyframe, removeKeyframe, setCurve, transitionRange, withLane } from "./lanes";
import { type KeySelection, type LaneEditing, type LaneGroup, type LaneRowSpec, LaneStack } from "./LaneStack";
import { MixChecks } from "./MixChecks";
import { PlanTimeline } from "./PlanTimeline";
import { PlanSession, type PlanSide } from "./session";
import { PlanRibbon, SimpleControls } from "./SimpleControls";
import { applyStyle, type SimpleSettings, settingsOf } from "./styles";

type EditorView = "simple" | "advanced" | "manual";

const VIEWS: { id: EditorView; label: string; title: string }[] = [
  { id: "simple", label: "Simple", title: "A style, a length and how much of it" },
  { id: "advanced", label: "Advanced", title: "A curve per stem, on the stem's waveform" },
  { id: "manual", label: "Manual", title: "Every lane and keyframe" },
];
const VIEW_KEY = "seedbox2.mix.view";
const SAVE_AFTER_MS = 600;
const WAVE_RETRY_MS = 1500;
const ADVANCED_ROW_H = 56;
const MANUAL_ROW_H = 40;
const DECKS: DeckId[] = ["a", "b"];

const LANE_LABEL: Record<LaneName, string> = {
  volume: "Fader",
  eq_low: "EQ low",
  eq_mid: "EQ mid",
  eq_high: "EQ high",
  filter: "Filter",
  filter_res: "Filter resonance",
  echo_send: "Echo send",
  echo_feedback: "Echo feedback",
  reverb_send: "Reverb send",
  gain_drums: "Drums",
  gain_bass: "Bass",
  gain_vocals: "Vocals",
  gain_other: "Other",
};

interface Loaded {
  engine: LiveEngine;
  session: PlanSession;
}

/** A deck's waveform: the first request starts the job when there isn't one yet, so it comes a little later. */
function useDeckWave(hash: string): WaveData | null {
  const [wave, setWave] = useState<WaveData | null>(null);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let gone = false;
    let tries = 0;
    const attempt = () => {
      loadWave(apiUrl(`/api/tracks/${hash}/waveform`)).then(
        (w) => !gone && setWave(w),
        () => {
          if (!gone && ++tries < 80) timer = setTimeout(attempt, WAVE_RETRY_MS);
        },
      );
    };
    attempt();
    return () => {
      gone = true;
      clearTimeout(timer);
    };
  }, [hash]);
  return wave;
}

/** The track's camelot key, for the key check. Null while it's unknown. */
function useDeckKey(hash: string): string | null {
  const [key, setKey] = useState<string | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    api.key.status(hash, { quiet: true, signal: ctl.signal }).then(
      (s) => setKey("effective" in s ? s.effective.camelot : null),
      () => {},
    );
    return () => ctl.abort();
  }, [hash]);
  return key;
}

async function open(from: string, to: string, signal: { gone: boolean }): Promise<Loaded | null> {
  const { LiveEngine } = await import("@/audio/engine");
  const { plan, auto } = await api.mixes.livePlan(from, to);
  const engine = new LiveEngine();
  await engine.load("a", { hash: plan.a.track_id });
  await engine.load("b", { hash: plan.b.track_id });
  if (signal.gone) {
    void engine.close();
    return null;
  }
  const session = new PlanSession(engine, auto ?? plan, auto ? plan : null);
  engine.schedule(session.plan);
  return { engine, session };
}

export function TransitionEditor({ from, to }: { from: string; to: string }) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const signal = { gone: false };
    let engine: LiveEngine | null = null;
    setLoaded(null);
    setError(null);
    open(from, to, signal).then(
      (l) => {
        if (!l) return;
        engine = l.engine;
        setLoaded(l);
      },
      (e: unknown) => !signal.gone && setError(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      signal.gone = true;
      void engine?.close();
    };
  }, [from, to]);

  if (error) return <div className="p-4 text-sm text-fg-muted">Couldn't open this plan: {error}</div>;
  if (!loaded) {
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-fg-muted">
        <Spinner /> Loading the plan
      </div>
    );
  }
  return <Editor key={`${from}/${to}`} from={from} to={to} {...loaded} />;
}

type SaveState = "idle" | "saving" | "saved" | "failed";

/** Saves the session's edit a moment after it stops changing, one request at a time, and once more on the way out. */
function useAutosave(session: PlanSession, revision: number, from: string, to: string): { state: SaveState; error: string | null } {
  const [state, setState] = useState<SaveState>("idle");
  const [error, setError] = useState<string | null>(null);
  const saved = useRef<MixPlan | null>(session.edited);
  const queue = useRef<Promise<unknown>>(Promise.resolve());

  const flush = useCallback(() => {
    const edited = session.edited;
    if (edited === saved.current) return;
    saved.current = edited;
    setState("saving");
    queue.current = queue.current
      .then((): Promise<unknown> => (edited ? api.mixes.savePlan(from, to, edited) : api.mixes.resetPlan(from, to)))
      .then(
        () => {
          if (saved.current === edited) {
            setState("saved");
            setError(null);
          }
        },
        (e: unknown) => {
          setState("failed");
          setError(e instanceof Error ? e.message : String(e));
        },
      );
  }, [session, from, to]);

  useEffect(() => {
    if (session.edited === saved.current) return;
    const timer = setTimeout(flush, SAVE_AFTER_MS);
    return () => clearTimeout(timer);
  }, [revision, session, flush]);
  useEffect(() => flush, [flush]);
  return { state, error };
}

const SAVE_LABEL: Record<SaveState, string> = { idle: "", saving: "Saving…", saved: "Saved", failed: "Couldn't save" };

function Editor({ engine, session, from, to }: Loaded & { from: string; to: string }) {
  const hashA = session.auto.a.track_id;
  const hashB = session.auto.b.track_id;
  const waveA = useDeckWave(hashA);
  const waveB = useDeckWave(hashB);
  const waves = useMemo(() => ({ a: waveA, b: waveB }), [waveA, waveB]);
  const stemA = useStemWave(hashA).wave;
  const stemB = useStemWave(hashB).wave;
  const keyA = useDeckKey(hashA);
  const keyB = useDeckKey(hashB);

  const subscribeEngine = useCallback((cb: () => void) => engine.subscribe(cb), [engine]);
  const state: EngineState = useSyncExternalStore(subscribeEngine, () => engine.state);
  const canStemA = useSyncExternalStore(subscribeEngine, () => engine.hasStems("a"));
  const canStemB = useSyncExternalStore(subscribeEngine, () => engine.hasStems("b"));
  const canStem = useMemo(() => ({ a: canStemA, b: canStemB }), [canStemA, canStemB]);
  const stems = useMemo<Record<DeckId, StemWave | null>>(() => ({ a: canStemA ? stemA : null, b: canStemB ? stemB : null }), [canStemA, canStemB, stemA, stemB]);

  const subscribeSession = useCallback((cb: () => void) => session.subscribe(cb), [session]);
  const revision = useSyncExternalStore(subscribeSession, () => session.revision);
  const plan = session.plan;
  const planRef = useMemo(
    () =>
      ({
        get current() {
          return session.plan;
        },
      }) as RefObject<MixPlan>,
    [session],
  );

  const [view, setViewState] = useState<EditorView>(() => {
    const stored = localStorage.getItem(VIEW_KEY);
    return VIEWS.some((v) => v.id === stored) ? (stored as EditorView) : "simple";
  });
  const setView = (next: EditorView) => {
    setViewState(next);
    localStorage.setItem(VIEW_KEY, next);
  };
  const [loop, setLoopState] = useState<[number, number] | null>(null);
  const [selected, setSelected] = useState<KeySelection | null>(null);
  const save = useAutosave(session, revision, from, to);

  const setLoop = (next: [number, number] | null) => {
    setLoopState(next);
    if (next) engine.setLoop(next[0], next[1]);
    else engine.setLoop(null);
  };
  const playing = state.status === "playing" || state.status === "buffering";
  const toggle = () => {
    if (playing) {
      engine.pause();
    } else {
      player.stop();
      void engine.play();
    }
  };
  const range = transitionRange(plan);
  const looped = loop !== null && loop[0] === range[0] && loop[1] === range[1];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) && (target as HTMLInputElement).type !== "range") return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) session.redo();
        else session.undo();
      } else if (e.ctrlKey && e.key.toLowerCase() === "y") {
        e.preventDefault();
        session.redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [session]);

  const input: CheckInput = { plan, waves, stems, keys: { a: keyA, b: keyB } };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `plan` is a new object after every edit that counts
  const checks = useMemo(() => planChecks(input), [plan, waves, stems, keyA, keyB]);

  const editing: LaneEditing = {
    planRef,
    begin: () => session.begin(),
    edit: (next) => session.edit(next),
    end: () => session.end(),
    selected,
    onSelect: setSelected,
  };

  const changeSimple = (next: SimpleSettings, held: boolean) => {
    if (held) session.begin(true);
    else session.end();
    session.edit(applyStyle(session.plan, next, { stems: canStem, base: session.auto, aDuration: waveA?.duration ?? null }));
  };

  const sideButton = (s: PlanSide, label: string, disabled = false) => (
    <Button size="xs" variant={session.side === s ? "primary" : "secondary"} aria-pressed={session.side === s} disabled={disabled} onClick={() => session.setSide(s)}>
      {label}
    </Button>
  );
  const original = session.auto.edited ? "original" : "auto";

  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" icon={playing ? Pause : Play} loading={state.status === "buffering"} onClick={toggle}>
          {playing ? "Pause" : "Play"}
        </Button>
        <Button icon={Repeat} variant={looped ? "primary" : "secondary"} aria-pressed={looped} onClick={() => setLoop(looped ? null : range)}>
          Loop the transition
        </Button>
        {loop && !looped && (
          <Button variant="ghost" onClick={() => setLoop(null)}>
            Clear the loop
          </Button>
        )}
        <div className="ml-2 flex items-center gap-1" role="group" aria-label="Which plan plays">
          {sideButton("auto", session.auto.edited ? "A: Original" : "A: Auto")}
          {sideButton("edit", "B: Your edit", !session.edited)}
        </div>
        <div className="flex items-center gap-1">
          <IconButton icon={Undo2} label="Undo" disabled={!session.canUndo} onClick={() => session.undo()} />
          <IconButton icon={Redo2} label="Redo" disabled={!session.canRedo} onClick={() => session.redo()} />
          <Button size="xs" variant="ghost" icon={RotateCcw} disabled={!session.edited} onClick={() => session.reset()}>
            Reset to {original}
          </Button>
        </div>
        <span role="status" title={save.error ?? undefined} className={save.state === "failed" ? "text-xs text-danger" : "text-xs text-fg-subtle"}>
          {SAVE_LABEL[save.state]}
        </span>
        <div className="ml-auto">
          <Tabs variant="segmented" items={VIEWS} value={view} onChange={setView} />
        </div>
      </div>

      <div className="flex flex-col items-stretch gap-4 lg:flex-row lg:items-start">
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          {view === "simple" && <SimpleControls settings={settingsOf(plan)} onChange={changeSimple} onRelease={() => session.end()} />}
          {view === "simple" && <PlanRibbon plan={plan} />}
          <div className="sticky top-0 z-sticky bg-surface">
            <PlanTimeline planRef={planRef} engine={engine} waves={waves} loop={loop} onLoop={setLoop} />
          </div>
          {view === "advanced" && (
            <LaneStack groups={advancedGroups(plan, canStem, stems, waves)} rowHeight={ADVANCED_ROW_H} editing={editing} revision={revision} beat={() => engine.beat()} />
          )}
          {view === "manual" && <LaneStack groups={manualGroups(plan, canStem, stems, waves)} rowHeight={MANUAL_ROW_H} editing={editing} revision={revision} beat={() => engine.beat()} />}
          {view !== "simple" && <KeyframeInspector plan={plan} selected={selected} onSelect={setSelected} onEdit={(next) => session.edit(next)} />}
          <p className="text-xs text-fg-subtle">
            {view === "simple"
              ? "Drag the waveforms to scrub. Drag the ruler to loop a selection."
              : "Drag the waveforms to scrub, the ruler to loop. On a lane: drag a keyframe, double-click to add one, right-click to remove it, Alt-click to change its curve."}
          </p>
          {state.error && <p className="text-xs text-danger">{state.error}</p>}
        </div>
        <aside className="shrink-0 rounded-sm border border-line lg:w-80" aria-label="Checks">
          <h3 className="border-b border-line px-3 py-1.5 text-2xs font-semibold uppercase tracking-wide text-fg-subtle">Checks</h3>
          <MixChecks
            checks={checks}
            onFix={(check) => {
              const fixed = fixCheck(check.id, input);
              if (fixed && fixed !== plan) session.edit(fixed);
            }}
          />
        </aside>
      </div>
    </div>
  );
}

const STEM_ROW: Record<"gain_drums" | "gain_bass" | "gain_vocals" | "gain_other", keyof StemWave> = { gain_drums: "drums", gain_bass: "bass", gain_vocals: "vocals", gain_other: "other" };
const BAND_ROW: Record<"eq_low" | "eq_mid" | "eq_high", "low" | "mid" | "high"> = { eq_low: "low", eq_mid: "mid", eq_high: "high" };

function rowFor(deck: DeckId, lane: LaneName, stems: StemWave | null, wave: WaveData | null): LaneRowSpec {
  const p = wavePalette();
  const row: LaneRowSpec = { deck, lane, label: LANE_LABEL[lane] };
  if (lane in STEM_ROW) {
    const name = STEM_ROW[lane as keyof typeof STEM_ROW] as "drums" | "bass" | "vocals" | "other";
    row.color = p.stem[name];
    if (stems) {
      row.peaks = stems[name];
      row.pps = stems.pps;
    }
  } else if (lane in BAND_ROW) {
    const band = BAND_ROW[lane as keyof typeof BAND_ROW];
    row.color = p[band];
    if (wave) {
      row.peaks = wave[band];
      row.pps = wave.pps;
    }
  }
  return row;
}

const deckTitle = (id: DeckId, plan: MixPlan) => `Deck ${id.toUpperCase()}${plan[id].key_shift ? ` (${plan[id].key_shift > 0 ? "+" : ""}${plan[id].key_shift} st)` : ""}`;

function advancedGroups(plan: MixPlan, canStem: Record<DeckId, boolean>, stems: Record<DeckId, StemWave | null>, waves: Record<DeckId, WaveData | null>): LaneGroup[] {
  return DECKS.map((id) => {
    const lanes: LaneName[] = canStem[id] ? ["volume", ...STEM_LANES] : ["volume", "eq_low", "eq_mid", "eq_high"];
    const title = canStem[id] ? deckTitle(id, plan) : `${deckTitle(id, plan)}: no stems, so its EQ`;
    return { title, rows: lanes.map((lane) => rowFor(id, lane, stems[id], waves[id])) };
  });
}

function manualGroups(plan: MixPlan, canStem: Record<DeckId, boolean>, stems: Record<DeckId, StemWave | null>, waves: Record<DeckId, WaveData | null>): LaneGroup[] {
  return DECKS.map((id) => {
    const lanes: LaneName[] = canStem[id] || plan[id].stem_lanes ? [...LANES, ...STEM_LANES] : [...LANES];
    return { title: deckTitle(id, plan), rows: lanes.map((lane) => rowFor(id, lane, stems[id], waves[id])) };
  });
}

/** The chosen keyframe in numbers, for the exact value a drag can't hit. */
function KeyframeInspector({ plan, selected, onSelect, onEdit }: { plan: MixPlan; selected: KeySelection | null; onSelect: (s: KeySelection | null) => void; onEdit: (plan: MixPlan) => void }) {
  const keyframes = selected ? laneOf(plan[selected.deck], selected.lane) : [];
  const key: Keyframe | undefined = selected ? keyframes[selected.index] : undefined;
  if (!selected || !key) return <p className="text-xs text-fg-subtle">Click a keyframe to see it in numbers.</p>;
  const { deck, lane, index } = selected;
  const put = (next: Keyframe[]) => onEdit(withLane(plan, deck, lane, next));
  return (
    <div className="flex flex-wrap items-end gap-2 rounded-sm border border-line p-2" data-testid="keyframe-inspector">
      <span className="self-center text-xs font-medium text-fg-muted">
        Deck {deck.toUpperCase()}, {LANE_LABEL[lane]}
      </span>
      <Field label="Beat">
        <Input key={`b${key.beat}`} type="number" step={0.25} className="w-24" defaultValue={key.beat} onBlur={(e) => put(moveKeyframe(keyframes, lane, index, Number(e.target.value), key.value))} />
      </Field>
      <Field label="Value">
        <Input key={`v${key.value}`} type="number" step={0.01} className="w-24" defaultValue={Number(key.value.toFixed(3))} onBlur={(e) => put(moveKeyframe(keyframes, lane, index, key.beat, Number(e.target.value)))} />
      </Field>
      <Field label="Curve">
        <Select aria-label="Curve" value={key.curve} onChange={(e) => put(setCurve(keyframes, index, e.target.value as Keyframe["curve"]))}>
          {CURVES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </Select>
      </Field>
      <Button
        size="xs"
        variant="ghost"
        disabled={keyframes.length < 2}
        onClick={() => {
          onSelect(null);
          put(removeKeyframe(keyframes, index));
        }}
      >
        Remove
      </Button>
    </div>
  );
}
