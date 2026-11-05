/**
 * The booth: two stacked decks scrolling under one playhead column, a beat the same width on both, and what's coming along
 * the bottom. Nothing here renders per frame (PERFORMANCE.md): one loop, gated by `mayDraw`, paints the panels' canvases and
 * writes their text through refs; the waveforms have their own. React renders when the transition or the status changes.
 */
import { ArrowLeft, Maximize, Minus, Pause, Play, Plus } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { Deck, MixPlan } from "@/api/mixplan.generated";
import type { SessionTrack } from "@/api/types";
import { beatAtFile, deckBpm } from "@/audio/engine/position";
import type { PlayheadSource } from "@/audio/playhead";
import { Badge, IconButton, KeyBadge, Spinner } from "@/components/ui";
import { setupCanvas, STEMS, TrackWaveform, useStemWave, useTrackWave, wavePalette } from "@/features/waveform";
import { mayDraw } from "@/lib/frameCap";
import { fmtTime } from "@/lib/format";
import { useHotkey } from "@/lib/hotkeys";
import { navigate } from "@/lib/router";
import type { NowPlayingDriver } from "./driver";
import { bandLevels, blendSpan, deckAnalysis, deckRate, deckRegions, deckRole, fmtCountdown, heldTime, masterBpm, newStrip, type PlanSide, readStrip, stemLevels, techniqueLabel, timeLeft, upNext } from "./session";
import { ballistics, drawMeters, drawStrip } from "./strip";

const ZOOM_BARS = [4, 8, 16, 32, 64];
const DEFAULT_ZOOM = 2;
const METERS_H = 84;
const STRIP_H = 56;
const METER_W = 112;
const STRIP_W = 160;
/** Everything of a deck but its main waveform: the overview and the beat ruler, with their margins. */
const DECK_CHROME = 30 + 4 + 16 + 6;
const DECK_GAP = 8;

/** Runs painters once per frame that may draw, while the driver plays; once more on any coarse change. Counts for the bench. */
export const paintStats = { draws: 0 };
type Painter = (dt: number) => void;

function usePaintLoop(driver: NowPlayingDriver): (p: Painter) => () => void {
  const painters = useRef(new Set<Painter>());
  useEffect(() => {
    let raf = 0;
    let last = 0;
    const paint = (dt: number) => {
      paintStats.draws++;
      for (const p of painters.current) p(dt);
    };
    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      if (!mayDraw()) return;
      paint(last ? Math.min(0.25, (now - last) / 1000) : 0);
      last = now;
    };
    const sync = () => {
      if (driver.status() === "playing") {
        if (!raf) raf = requestAnimationFrame(loop);
        return;
      }
      cancelAnimationFrame(raf);
      raf = 0;
      last = 0;
      paint(1);
    };
    sync();
    const un = driver.subscribe(sync);
    return () => {
      un();
      cancelAnimationFrame(raf);
    };
  }, [driver]);
  return useCallback((p: Painter) => {
    painters.current.add(p);
    return () => painters.current.delete(p);
  }, []);
}

const setText = (el: HTMLElement | null, text: string) => {
  if (el && el.textContent !== text) el.textContent = text;
};

function deckSource(driver: NowPlayingDriver, side: () => PlanSide): PlayheadSource {
  return {
    time: () => {
      const plan = driver.plan();
      return plan ? heldTime(plan[side()], driver.beat()) : null;
    },
    playing: () => driver.status() === "playing",
    rate: () => {
      const plan = driver.plan();
      return plan ? deckRate(plan, plan[side()], driver.beat()) : 0;
    },
    subscribe: (listener) => driver.subscribe(listener),
  };
}

const ROLE_WORD = { cued: "Cued", playing: "Playing", gone: "Finished" };
const BAND_NAMES = ["LOW", "MID", "HI"];

interface DeckProps {
  driver: NowPlayingDriver;
  name: "A" | "B";
  track: SessionTrack;
  side: PlanSide;
  plan: MixPlan;
  bars: number;
  height: number;
  subscribePaint: (p: Painter) => () => void;
}

function DeckRow({ driver, name, track, side, plan, bars, height, subscribePaint }: DeckProps) {
  const assets = driver.assets?.(track.hash) ?? null;
  const fetchedWave = useTrackWave(assets ? null : track.hash);
  const fetchedStems = useStemWave(assets ? null : track.hash);
  const stems = assets?.stems ?? fetchedStems.wave;
  const deck: Deck = plan[side];
  const sideRef = useRef(side);
  sideRef.current = side;
  // once per track: the deck keeps its view when the next plan takes the track over
  const analysis = useMemo(() => assets?.analysis ?? deckAnalysis(track, deck), [track.hash]);
  const source = useMemo(() => deckSource(driver, () => sideRef.current), [driver]);
  const regions = useMemo(() => deckRegions(plan, side), [plan, side]);
  const span = (bars * 4 * 60) / deck.grid[0].bpm;

  const meters = useRef<HTMLCanvasElement>(null);
  const strip = useRef<HTMLCanvasElement>(null);
  const bpmEl = useRef<HTMLSpanElement>(null);
  const pitchEl = useRef<HTMLSpanElement>(null);
  const leftEl = useRef<HTMLSpanElement>(null);
  const roleEl = useRef<HTMLSpanElement>(null);

  const paintRef = useRef<Painter>(() => {});
  const levels = useRef(new Float32Array(4));
  const raw = useRef(new Float32Array(4));
  const reading = useRef(newStrip());
  const drawnMeter = useRef("");
  const drawnStrip = useRef("");
  paintRef.current = (dt) => {
    const p = driver.plan();
    if (!p) return;
    const beat = driver.beat();
    const d = p[side];
    const t = heldTime(d, beat);
    const s = readStrip(d, beat, reading.current);
    const hasStems = !!stems;
    if (hasStems) stemLevels(stems, d, t, beat, s, raw.current);
    else if (fetchedWave) bandLevels(fetchedWave, t, s, raw.current);
    else raw.current.fill(0);
    ballistics(levels.current, raw.current, dt);
    const pal = wavePalette();
    const meterKey = Array.from(levels.current, (v) => Math.round(v * 64)).join();
    if (meters.current && meterKey !== drawnMeter.current) {
      drawnMeter.current = meterKey;
      const c = setupCanvas(meters.current, METER_W, METERS_H);
      if (c) {
        if (hasStems) drawMeters(c.ctx, levels.current, ["DRM", "BAS", "VOC", "OTH"], STEMS.map((n) => pal.stem[n]), METER_W, METERS_H, pal);
        else drawMeters(c.ctx, levels.current, BAND_NAMES, [pal.low, pal.mid, pal.high], METER_W, METERS_H, pal);
      }
    }
    const stripKey = `${s.volume.toFixed(2)} ${s.eqLow.toFixed(1)} ${s.eqMid.toFixed(1)} ${s.eqHigh.toFixed(1)} ${s.filter.toFixed(2)}`;
    if (strip.current && stripKey !== drawnStrip.current) {
      drawnStrip.current = stripKey;
      const c = setupCanvas(strip.current, STRIP_W, STRIP_H);
      if (c) drawStrip(c.ctx, s, STRIP_W, STRIP_H, pal);
    }
    const bpm = masterBpm(p, Math.max(beat, d.play_from_beat));
    const own = deckBpm(d, beat);
    setText(bpmEl.current, bpm.toFixed(1));
    const pitch = (bpm / own - 1) * 100;
    setText(pitchEl.current, `${pitch >= 0 ? "+" : "−"}${Math.abs(pitch).toFixed(1)} %`);
    setText(leftEl.current, `−${fmtTime(timeLeft(p, d, track.duration, beat))}`);
    setText(roleEl.current, ROLE_WORD[deckRole(d, beat)]);
  };
  useEffect(() => subscribePaint((dt) => paintRef.current(dt)), [subscribePaint]);
  useLayoutEffect(() => {
    // the first paint of a track, and the meters' other shape when its stems arrive
    drawnMeter.current = drawnStrip.current = "";
    paintRef.current(1);
  });

  const seek = (t: number) => {
    const p = driver.plan();
    if (p) driver.seek(beatAtFile(p[side], t));
  };

  return (
    <section aria-label={`Deck ${name}`} className="flex min-h-0 gap-3 rounded-md border border-line bg-panel p-3">
      <div className="flex w-72 shrink-0 flex-col gap-2 overflow-hidden">
        <div className="flex items-start gap-2">
          <span className="flex size-6 shrink-0 items-center justify-center rounded-xs bg-raised text-sm font-semibold text-fg">{name}</span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-md font-medium text-fg">{track.title ?? track.hash.slice(0, 12)}</div>
            <div className="truncate text-xs text-fg-subtle">{track.artist ?? ""}</div>
          </div>
          <span ref={roleEl} className="label-caps shrink-0" />
        </div>
        <div className="flex items-baseline gap-3 num">
          <span>
            <span ref={bpmEl} className="text-xl font-semibold text-fg" />
            <span className="ml-1 text-2xs text-fg-subtle">BPM</span>
          </span>
          <span ref={pitchEl} className="text-xs text-fg-subtle" title="Off the track's own tempo" />
          <KeyBadge camelot={track.key.camelot} keyName={track.key.key ?? undefined} size="sm" />
          {deck.key_shift !== 0 && <span className="text-xs text-fg-subtle">{`${deck.key_shift > 0 ? "+" : "−"}${Math.abs(deck.key_shift)} st`}</span>}
          <span ref={leftEl} className="ml-auto text-md text-fg" title="Time left in this track" />
        </div>
        <div className="flex items-end justify-between gap-2">
          <canvas ref={meters} role="img" aria-label="Stem meters" style={{ width: METER_W, height: METERS_H }} />
          <canvas ref={strip} role="img" aria-label="EQ, filter and fader from the plan" style={{ width: STRIP_W, height: STRIP_H }} />
        </div>
      </div>
      <TrackWaveform
        className="min-w-0 flex-1"
        analysis={analysis}
        wave={assets ? undefined : fetchedWave}
        trackId={track.hash}
        source={source}
        deck
        bare
        follow="centred"
        phraseBands
        regions={regions}
        span={span}
        height={height}
        onSeek={seek}
      />
    </section>
  );
}

function UpNextStrip({ driver, index, subscribePaint }: { driver: NowPlayingDriver; index: number; subscribePaint: (p: Painter) => () => void }) {
  const { session, chain } = driver;
  const line = useRef<HTMLSpanElement>(null);
  const titleEl = useRef<HTMLSpanElement>(null);
  const styleEl = useRef<HTMLSpanElement>(null);
  const countEl = useRef<HTMLSpanElement>(null);
  const countLabel = useRef<HTMLSpanElement>(null);
  const paint = useRef<Painter>(() => {});
  paint.current = () => {
    const n = upNext(session.tracks, chain, index, driver.beat());
    const name = (t: SessionTrack) => [t.title, t.artist].filter(Boolean).join(" · ") || t.hash.slice(0, 12);
    if (n.kind === "last") {
      setText(line.current, "Up next");
      setText(titleEl.current, "Nothing: this is the last track of the set");
      setText(styleEl.current, "");
      setText(countEl.current, "");
      setText(countLabel.current, "");
    } else if (n.kind === "stops") {
      setText(line.current, "Up next");
      setText(titleEl.current, `No plan into ${name(n.track)}: the set stops after this track`);
      setText(styleEl.current, "");
      setText(countEl.current, "");
      setText(countLabel.current, "");
    } else {
      setText(line.current, n.kind === "blending" ? "Mixing in" : "Up next");
      setText(titleEl.current, name(n.track));
      setText(styleEl.current, `${techniqueLabel(n.plan)}${n.plan.bars ? `, ${n.plan.bars} bars` : ""}`);
      setText(countEl.current, fmtCountdown(n.beats));
      setText(countLabel.current, n.kind === "blending" ? "bars left" : "bars to go");
    }
  };
  useEffect(() => subscribePaint(() => paint.current(0)), [subscribePaint]);
  useLayoutEffect(() => paint.current(0));
  return (
    <section aria-label="Up next" className="flex h-14 shrink-0 items-center gap-4 rounded-md border border-line bg-panel px-4">
      <span ref={line} className="label-caps shrink-0" />
      <span ref={titleEl} className="min-w-0 flex-1 truncate text-md text-fg" />
      <span ref={styleEl} className="shrink-0 text-sm text-fg-muted" />
      <span className="flex shrink-0 items-baseline gap-1.5 num">
        <span ref={countEl} className="text-2xl font-semibold text-fg" />
        <span ref={countLabel} className="text-xs text-fg-subtle" />
      </span>
    </section>
  );
}

/** The decks' tracks for a transition: track i sits on A when i is even, so a track doesn't move when the next plan takes it. */
function decksOf(index: number): { A: { track: number; side: PlanSide }; B: { track: number; side: PlanSide } } {
  const out = { track: index, side: "a" as const };
  const inc = { track: index + 1, side: "b" as const };
  return index % 2 === 0 ? { A: out, B: inc } : { A: inc, B: out };
}

export function NowPlaying({ driver }: { driver: NowPlayingDriver }) {
  const version = useRef(0);
  const subscribe = useCallback(
    (cb: () => void) =>
      driver.subscribe(() => {
        version.current++;
        cb();
      }),
    [driver],
  );
  useSyncExternalStore(subscribe, () => version.current);
  const status = driver.status();
  const index = driver.index();
  const plan = driver.plan();
  const error = driver.error();
  const [zoom, setZoom] = useState(DEFAULT_ZOOM);
  const subscribePaint = usePaintLoop(driver);

  const area = useRef<HTMLDivElement>(null);
  const [areaH, setAreaH] = useState(0);
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    setAreaH(el.clientHeight);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setAreaH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const waveH = Math.max(96, Math.floor((areaH - DECK_GAP) / 2) - DECK_CHROME - 26);

  const playing = status === "playing" || status === "buffering";
  const toggle = () => (playing ? driver.pause() : driver.play());
  useHotkey("Space", toggle);
  useHotkey("Escape", () => navigate({ name: "sets" }));
  useHotkey(["=", "+"], () => setZoom((z) => Math.max(0, z - 1)));
  useHotkey("-", () => setZoom((z) => Math.min(ZOOM_BARS.length - 1, z + 1)));

  const decks = decksOf(index);
  const blend = plan ? blendSpan(plan) : null;
  return (
    <div className="flex h-full flex-col gap-2 bg-bg p-2" data-bench="now-playing">
      <header className="flex h-10 shrink-0 items-center gap-2 px-1">
        <IconButton icon={ArrowLeft} label="Leave the decks" tooltip="Leave the decks (Esc)" onClick={() => navigate({ name: "sets" })} />
        <IconButton icon={playing ? Pause : Play} label={playing ? "Pause" : "Play"} tooltip={`${playing ? "Pause" : "Play"} (Space)`} size="lg" variant="primary" loading={status === "buffering"} disabled={!plan || !!error} onClick={toggle} />
        <div className="min-w-0 flex-1 truncate">
          <span className="text-md font-medium text-fg">{driver.session.name}</span>
          <span className="ml-2 text-xs text-fg-subtle num">{`Track ${index + 1} of ${driver.session.tracks.length}`}</span>
        </div>
        {error && <Badge tone="danger">{error}</Badge>}
        {status === "ended" && <Badge tone="neutral">The set has ended</Badge>}
        {blend && <span className="sr-only">{`Transition from beat ${blend.start} to ${blend.end}`}</span>}
        <IconButton icon={Minus} label="Zoom out (−)" size="sm" disabled={zoom === ZOOM_BARS.length - 1} onClick={() => setZoom((z) => z + 1)} />
        <span className="w-12 text-center text-xs text-fg-subtle num">{`${ZOOM_BARS[zoom]} bars`}</span>
        <IconButton icon={Plus} label="Zoom in (=)" size="sm" disabled={zoom === 0} onClick={() => setZoom((z) => z - 1)} />
        <IconButton icon={Maximize} label="Full screen" size="sm" onClick={() => void document.documentElement.requestFullscreen?.()} />
      </header>
      <div ref={area} className="grid min-h-0 flex-1 grid-rows-2 gap-2">
        {plan ? (
          (["A", "B"] as const).map((name) => {
            const d = decks[name];
            const track = driver.session.tracks[d.track];
            return <DeckRow key={`${name}${d.track}`} driver={driver} name={name} track={track} side={d.side} plan={plan} bars={ZOOM_BARS[zoom]} height={waveH} subscribePaint={subscribePaint} />;
          })
        ) : (
          <div className="row-span-2 flex items-center justify-center">
            <Spinner />
          </div>
        )}
      </div>
      <UpNextStrip driver={driver} index={index} subscribePaint={subscribePaint} />
    </div>
  );
}
