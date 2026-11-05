/**
 * A saved plan on the live engine: play it, scrub it, loop the transition or any selection, drag its keyframes while it
 * plays and A/B the result against the plan as it came. Nothing here waits for a render: the transition editor (#79)
 * builds on this.
 */
import { Pause, Play, Repeat, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api, apiUrl } from "@/api/client";
import type { MixPlan } from "@/api/mixplan.generated";
import type { EngineState, LiveEngine } from "@/audio/engine";
import { player } from "@/audio/player";
import { Button, Select, Spinner } from "@/components/ui";
import { loadWave } from "@/features/waveform/waveData";
import type { WaveData } from "@/features/waveform/prepare";
import { type DeckId, type LaneName, laneNames, transitionRange } from "./lanes";
import { PlanTimeline } from "./PlanTimeline";
import { PlanSession, type PlanSide } from "./session";

interface Loaded {
  engine: LiveEngine;
  session: PlanSession;
  userEdited: boolean;
}

const WAVE_RETRY_MS = 1500;

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

const LANE_LABEL: Record<LaneName, string> = {
  volume: "Volume",
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

async function open(from: string, to: string, signal: { gone: boolean }): Promise<Loaded | null> {
  const { LiveEngine } = await import("@/audio/engine");
  const { plan, user_edited } = await api.mixes.livePlan(from, to);
  const engine = new LiveEngine();
  await engine.load("a", { hash: plan.a.track_id });
  await engine.load("b", { hash: plan.b.track_id });
  if (signal.gone) {
    void engine.close();
    return null;
  }
  engine.schedule(plan);
  return { engine, session: new PlanSession(engine, plan), userEdited: user_edited };
}

export function LivePlanPanel({ from, to }: { from: string; to: string }) {
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
  return <Transport key={`${from}/${to}`} {...loaded} />;
}

function Transport({ engine, session, userEdited }: Loaded) {
  const waveA = useDeckWave(session.auto.a.track_id);
  const waveB = useDeckWave(session.auto.b.track_id);
  const waves = useMemo(() => ({ a: waveA, b: waveB }), [waveA, waveB]);
  const subscribeEngine = useCallback((cb: () => void) => engine.subscribe(cb), [engine]);
  const state: EngineState = useSyncExternalStore(subscribeEngine, () => engine.state);
  const subscribeSession = useCallback((cb: () => void) => session.subscribe(cb), [session]);
  const side = useSyncExternalStore(subscribeSession, () => session.side);
  const hasEdit = useSyncExternalStore(subscribeSession, () => session.edited !== null);

  const planRef = useRef<MixPlan>(session.plan);
  planRef.current = session.plan;
  const [deck, setDeck] = useState<DeckId>("a");
  const [lane, setLane] = useState<LaneName>("volume");
  const [loop, setLoopState] = useState<[number, number] | null>(null);

  const setLoop = (next: [number, number] | null) => {
    setLoopState(next);
    if (next) engine.setLoop(next[0], next[1]);
    else engine.setLoop(null);
  };
  const onEdit = (plan: MixPlan) => {
    session.edit(plan);
    planRef.current = plan;
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
  const range = transitionRange(session.plan);
  const looped = loop !== null && loop[0] === range[0] && loop[1] === range[1];
  const sideButton = (s: PlanSide, label: string, disabled = false) => (
    <Button size="xs" variant={side === s ? "primary" : "secondary"} aria-pressed={side === s} disabled={disabled} onClick={() => session.setSide(s)}>
      {label}
    </Button>
  );

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
          {sideButton("auto", userEdited ? "A: Saved" : "A: Auto")}
          {sideButton("edit", "B: Your edit", !hasEdit)}
          {hasEdit && (
            <Button size="xs" variant="ghost" icon={RotateCcw} onClick={() => session.reset()}>
              Discard
            </Button>
          )}
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          <Select aria-label="Deck" className="h-control-sm" value={deck} onChange={(e) => setDeck(e.target.value as DeckId)}>
            <option value="a">Deck A</option>
            <option value="b">Deck B</option>
          </Select>
          <Select aria-label="Lane" className="h-control-sm" value={lane} onChange={(e) => setLane(e.target.value as LaneName)}>
            {laneNames(session.plan[deck]).map((name) => (
              <option key={name} value={name}>
                {LANE_LABEL[name]}
              </option>
            ))}
          </Select>
        </div>
      </div>
      <PlanTimeline planRef={planRef} engine={engine} waves={waves} deck={deck} lane={lane} loop={loop} onLoop={setLoop} onEdit={onEdit} />
      <p className="text-xs text-fg-subtle">
        Drag the waveforms to scrub. Drag the ruler to loop a selection. Drag a keyframe, or double-click to add one: you hear it as you move it.
      </p>
      {state.error && <p className="text-xs text-danger">{state.error}</p>}
    </div>
  );
}
