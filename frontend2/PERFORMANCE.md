# Frontend performance rules

Seedbox 2's UI budget (`seedbox2/README.md`): 60 fps while scrolling, zooming and playing, and no long tasks over
50 ms, on an 8 GB M1 Air or a 4-core x86 laptop with integrated graphics. These rules keep it there. The benchmark
checks them: `python -m seedbox2.bench frontend` (`seedbox2/bench/README.md`).

## 1. Nothing re-renders React per animation frame

Anything that moves with the clock or the pointer (playheads, zoom, scroll, hover readouts, meters, the audio
engine's state) is drawn from refs: a canvas, WebGL, or direct DOM writes to an element held in a ref. React renders
when the *structure* changes (another track, a panel opens), not 60 times a second.

- **Playheads** read `player.time()` (the audio clock now) in their own `requestAnimationFrame` loop, which runs only
  while their track plays. The player store's `position` changes 25 times a second: don't select it in a component.
- **Zoom and scroll** live in a view controller (`features/waveform/hooks.ts`: `useViewCtl`). Canvases subscribe and
  redraw on the next frame (`useFrame` coalesces any number of changes per frame into one draw). Components hear
  about coarse facts only, with `useViewFact(ctl, (v) => v.span < duration)`: it re-renders when the boolean flips.
- **Hover readouts** (time, bar) are elements that pointer handlers move and fill through refs, not `useState`.
- **Labels that change with the view** ("12 bars visible") are written with `textContent` in the draw.
- An SVG with hundreds of elements is fine to render once; it must never take per-frame props
  (`PeaksWaveform` takes a `trackId` and moves its own playhead).

Measured: the benchmark counts React commits (`react_commits_per_s`). The waveform scenarios (zoom, scroll, hover,
play) are held to 5 a second at most; they ran at 30–60 before this rule.

## 2. Narrow zustand selectors

A component re-renders whenever its selector's result changes. Select the fields you show, one per call, or wrap a
selector that builds an object or array in `useShallow`. Read state outside React with `useStore.getState()` and
`useStore.subscribe` (event handlers, draw loops).

```ts
const connection = useApp((s) => s.connection);              // yes
const playing = usePlayer((s) => s.currentId === id && s.status === "playing");  // yes: a boolean
const app = useApp();                                        // no: the whole store
const { a, b } = useApp((s) => ({ a: s.a, b: s.b }));        // no: a new object each time
```

`src/store/subscriptions.test.ts` (in `deno task test`) fails on whole-store subscriptions, object/array selectors
without `useShallow` and per-frame fields such as the player's `position`.

## 3. Long lists are virtualised

A table or list that can grow past a screenful (library, crates, set list, recommendations, jobs) renders only the
rows in view: `VirtualRows` inside a `TableScroll`, or `VirtualList` (`components/ui/Virtual.tsx`,
`@tanstack/react-virtual`). Rows have a fixed height and are memoised (`React.memo`), so scrolling renders only the
rows that come into view. `#/ui` and `#/bench` show 10,000 rows.

Something drawn in every row (the library's overview waveforms) is one canvas over the column, not a canvas per row:
`features/library/LibraryTable.tsx` draws the visible rows' cached images into it on the next frame after a scroll.

## 4. Heavy data work happens in a worker

Preparing waveforms (normalising bands) and decoding the binary waveform file run in `features/waveform/wave.worker.ts`;
results come back as transferred `Float32Array`s, cached per waveform. Use `useWave(analysis)` or
`loadWave(url)`, not `prepareWave` (the synchronous version, for tests). New heavy work on track data (parsing large
responses, beat grids for a whole set, peaks for stems) goes in a worker the same way.

## 5. Measure

```sh
(cd frontend2 && deno task build)
.venv/bin/python -m seedbox2.bench frontend --cpu-throttle 4   # ~ a slow laptop
```

Each scenario reports fps, the 95th-percentile frame time, long tasks and React commits a second; `report` marks
the budgets ✅/❌. Run it before and after a change that touches what moves on screen.
