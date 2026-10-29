# Seedbox 2 frontend — design system & conventions

> Copied from `frontend/DESIGN.md` when `frontend2/` was forked (#36). The tokens, components and conventions apply
> as written. The sections on Seedbox 1's pages, mock mode and `deno task screenshot` describe `frontend/` only:
> frontend2 has no mock mode yet, its dev server is on :5174 and it proxies `/api` to 127.0.0.1:8766. The design
> language (tokens, sizes, motion, colour roles) is in `DESIGN2.md` (#71), which replaces the Tokens section here.
> frontend2's tests are under [Tests](#tests-frontend2) at the end.

React 19 + TypeScript (strict) + Vite 8 + Tailwind 4, built with **Deno 2.9** (no Node). Read this before adding screens.

## Commands (run in `frontend/`)

| Task | What |
|---|---|
| `deno install` | install npm deps into `node_modules/` (after editing package.json) |
| `deno task dev` | Vite on :5173, proxies `/api` (incl. SSE) → `http://127.0.0.1:8765` |
| `deno task dev:mock` | same, but every API call/SSE event is served by `src/mock` (no backend) |
| `deno task build` | typecheck + production build → `dist/` (relative asset paths, servable from any origin) |
| `deno task build:mock` / `preview:mock` | mock build → `dist-mock/`, served on :4174 (for screenshots) |
| `deno task typecheck` | `tsc` 7 (strict, noUnused*) — must be clean |
| `deno task lint` | `deno lint` (recommended + react/jsx rules; the Deno-runtime `no-window*` and `no-sloppy-imports` rules are off) — must be clean |
| `deno task test` / `test:watch` | vitest unit tests (`src/**/*.test.ts`, next to the module they test; node environment, `@/` alias from `vite.config.ts`) |
| `deno task screenshot` | build mock mode and screenshot `#/crates`, a crate, the set builder and the mix view at 1440×900 and 1180×760 → `screenshots/` (git-ignored); `--no-build` reuses `dist-mock/`, `--out=DIR` writes elsewhere, `CHROME=…` picks the browser |
| `deno task fixtures:engine` | regenerate `src/mock/fixtures/engine.json` by running the Python engine (needs numpy + scipy): after a change to scoring, ordering, transitions, the set plan or mix plans |

`src/mock/fixtures/tunebat.json` (the tunebat tracks and search page behind mock mode) is hand-maintained: it was
generated once from captured tunebat responses that are no longer in the repo, so edit it directly; run
`deno task fixtures:engine` afterwards so `engine.json` follows.

Real backend for dev: `.venv/bin/python -m seedbox.server --port 8765 --dev` (from the project root).
Deno enforces a 24 h minimum npm package age — use caret ranges, don't pin brand-new versions.

**Screenshots:** `deno task screenshot` covers the main screens (it serves `dist-mock/` itself and runs Chrome
like below). For other routes: `deno task build:mock && deno task preview:mock`, then
`chrome-headless-shell --headless --hide-scrollbars --screenshot=x.png --window-size=1440,900 --virtual-time-budget=5000 --autoplay-policy=no-user-gesture-required "http://localhost:4174/#/crates"`.
Against the real backend the open SSE stream stalls `--virtual-time-budget`: use `--timeout=6000` and kill the
process if it hangs. `#/ui` is the component gallery; `#/ui?demo=activity` (mock) opens a crate, starts a job and
plays a preview, so SSE-driven chrome (preview/job indicators, toasts, mini player) shows up in a screenshot.

## Layout

```
src/
  api/types.ts        LEAD — the wire contract (never edit; propose changes)
  api/client.ts       typed fetch wrappers for every endpoint (`api.*`), ApiError, mock switch
  api/events.ts       useServerEvents() — the single SSE connection → store
  store/app.ts        global zustand store (previews, jobs, analysis, setVersion, crates, system, settings, selection)
  store/toasts.ts     toast.info/success/warning/error()
  store/system.ts     yt-dlp update action/state
  audio/player.ts     THE audio player (one <audio>), usePlayer(), useIsPlaying()
  lib/                camelot.ts (colours/moves) · moves.ts · format.ts · router.tsx · hotkeys.ts ·
                      focusSearch.ts (⌘K protocol) · useAsync.ts · useListNav.ts · theme.ts (canvas colours) · cn.ts
  components/ui/      the design system — import from "@/components/ui"
  components/shell/   AppShell, Sidebar, TopBar, MiniPlayer, Toaster, GlobalHotkeys, usePageTitle
  pages/              one file per route (see ownership)
  features/crate/     FRONTEND-CRATE (wave 2)
  features/set/, features/waveform/   FRONTEND-SET (wave 2)
  mock/               fake backend for VITE_MOCK=1 (server.ts, data.ts, analysis.ts, audio.ts, fixtures/)
```

**Ownership.** Wave-2 agents own only: CRATE → `pages/Search*`, `pages/Crate*`, `pages/Downloads*`,
`features/crate/**`; SET → `pages/SetBuilder*`, `features/set/**`, `features/waveform/**`. Everything else
(package.json, configs, ui/, shell/, store/, api/client.ts, lib/, mock/) is FRONTEND-FOUNDATION's — don't edit it;
put feature-specific components/hooks in your feature folder and ask the lead for foundation changes (a missing
ui prop, a mock behaviour). Deps already installed for you: `@dnd-kit/core`, `@dnd-kit/sortable`,
`@dnd-kit/utilities`, `lucide-react`, `zustand`. Don't add dependencies.

Route components and their props are fixed (the router in `App.tsx` renders them, keyed by the param):
`SearchPage()`, `CratePage({ seedId })`, `DownloadsPage({ jobId })`, `SetBuilderPage({ folder })`.

## Tokens (`src/index.css` `@theme`)

See `DESIGN2.md`: type, spacing, control heights, radii, elevation and z-index, motion, and the colour roles
(surfaces, text, accent, status, music). `src/design-tokens.test.ts` rejects arbitrary values and hex colours
outside `index.css`. Utilities: `num` (tabular numerals — use on every BPM/time/score/count), `label-caps`,
`scroll-thin`. For `<canvas>`, read resolved values with `cssVar()` (`lib/theme.ts`) or `wavePalette()`.

## Camelot & moves (single source: `lib/camelot.ts`, `lib/moves.ts`)

- `keyColor(code)` → `{ base, bg, border, solidFg }` for any Camelot code (12 hues around the wheel, Mixed-In-Key
  order 1 = teal … 4 = yellow … 7 = red … 10 = violet … 12 = sky; A = deeper, B = lighter; unknown = grey).
  Also exposed as CSS vars `--camelot-1A` / `--camelot-1A-bg`. Never hard-code key colours.
- `camelotMove(a, b)` = mixing.py `_camelot_move` (the `Move` union); `camelotAdjacent`, `parseCamelot`,
  `keyNameToCamelot("G♯ minor") → "1A"`.
- `moveColor(move)` (CSS var string), `moveColorValue(move)` (canvas), `MOVE_LABEL`, `MOVE_HELP`, `MOVE_TONE`.

## Components (`@/components/ui`)

| Component | Key props |
|---|---|
| `Button` | `variant` primary\|secondary\|outline\|ghost\|danger · `size` xs\|sm\|md\|lg · `icon`, `iconRight` (lucide) · `loading` · `kbd="Mod+D"` |
| `IconButton` | `icon`, `label` (aria + tooltip), `tooltip`, `variant`, `size`, `active`, `loading` |
| `Badge` | `tone` neutral\|accent\|ok\|warn\|danger\|info\|boost · `variant` soft\|solid\|outline · `size` xs\|sm · `icon` |
| `KeyBadge` | `camelot`, `keyName` (tooltip), `size` xs\|sm\|md, `variant` soft\|solid\|text, `move` (colour by move instead), `dim` |
| `ScoreChip` | `value` 0-100, `move`, `passed` (false → outlined/dimmed), `size` |
| `MoveChip` | `move`, `size`, `showHelp` |
| `StatusDot` | `tone`, `pulse`, `label` |
| `Table` kit | `TableScroll` (scroll container, sticky header) › `Table` › `THead`/`TBody` › `Tr` (`selected`, `dim`, `active`, `interactive`, `rowHeight`=30) › `Th` (`align`, `width`, `sort`, `onSort`) / `Td` (`align`, `num`, `muted`, `truncate`); `nextSort()` helper |
| `Checkbox` | `checked`, `onChange(checked, event)` (doesn't bubble to the row; `event.shiftKey` for range ticks), `indeterminate`, `label`, `color` |
| `Tabs` | `items` [{id,label,icon,count}], `value`, `onChange`, `variant` underline\|segmented |
| `Switch` | `checked`, `onChange`, `label`, `size` sm\|md, `disabled` (boolean settings) |
| `ProgressBar` | `value` 0-1 or null (indeterminate), `tone`, `size` xs\|sm\|md |
| `Spinner` | `size` · `Slider` `value`, `onChange`, `onCommit`, `min`/`max`/`step`, `color` |
| `Modal` | `open`, `onClose`, `title`, `description`, `footer`, `size` sm\|md\|lg\|xl (native `<dialog>`; global hotkeys pause while open) |
| `Tooltip` | `content`, `side`, `delay` (portal; wraps child in an inline-flex span) |
| `Kbd` | `combo="Mod+S"` (→ ⌘S) or children |
| `EmptyState` | `icon`, `title`, `description`, `action`, `compact` |
| `CoverArt` | `src` (Spotify URL or `/api/covers/<folder>/<id>` embedded art — both handled), `size` px, `camelot` (tinted fallback), `fill`, `rounded` — lazy, picks the right Spotify size |
| `StarRating` | `rating` 1-10 (→ stars like POPM) or `stars` 0-5 |
| `PeaksWaveform` | `peaks` (PreviewStatus.peaks), `progress` 0-1, `onSeek(f)`, `height`, `color`, `playedColor`, `loading` — preview clips only |
| `CamelotWheel` | `size`, `counts` {code: n}, `active`, `path` (set order), `onSelect`, `center` |
| Layout | `Page` › `PageHeader` (`title`, `subtitle`, `leading`, `actions`, children = toolbar row) › `PageBody` (scrolls; `padded`) · `Panel` + `PanelHeader` · `InfoRow` |
| Forms | `Input` (`icon`, `suffix`, `size` sm\|md\|lg, `invalid`), `Select`, `Field` (`label`, `hint`, `error`, `aside`) |

## Data

- **API**: `import { api } from "@/api/client"`. One function per endpoint returning types.ts shapes, e.g.
  `api.search(term, page)`, `api.newCrate(seedId)`, `api.prioritizePreview(id)`, `api.startJob(seedId, ids)`,
  `api.jobs()`, `api.job(id)`, `api.cancelJob(id)`, `api.crates()`, `api.crateSet(folder)`,
  `api.saveOrder(folder, ids)`, `api.autoOrder(folder)`, `api.partners(folder, id, "out"|"in")`,
  `api.analysis(folder, id)` (→ `null` while queued/running; throws ApiError 422 if analysis failed; never toasts),
  `api.reveal(path)`, `api.pickFolder(initial)` (native dialog → `{path|null}`; 501 outside the app window),
  `api.system()`, `api.updateYtdlp()`, `api.settings()`, `api.saveSettings(patch)`.
  Every call takes `{ quiet?, signal? }`. Failures throw `ApiError` (`status`, `message` = server `detail`,
  `isNetwork`, `isNotFound`, `isConflict` — 409 = crate has a running job) **and toast**, unless `quiet: true`.
  Audio/file URLs from the API (`audio_url`) go to the player as-is; `Track.cover_url` may be a remote Spotify
  URL or `/api/covers/<folder>/<track_id>` (art embedded in the MP3) — render it with `CoverArt`, or `apiUrl(path)`.
- **Store**: `useApp((s) => …)`. Live, server-pushed: `previews[id]`, `jobs[id]`, `analysis[folder][id]`,
  `setVersion[folder]`, `crates`, `system`, `settings`, `connection`, `epoch` (bumps on every SSE reconnect —
  put it in fetch deps), `selection`. Hooks: `usePreview(id)`, `useJob(id)`, `useActiveJobs()`,
  `useAnalysisState(folder, id)`, `useSetRefreshKey(folder)` (debounced key for refetching a CrateSet after
  `set_changed`/analysis bursts), `useDebounced(v, ms)`. After `api.newCrate`, previews arrive via SSE; after
  `api.startJob`, call `useApp.getState().upsertJob(job)` then `navigate({ name: "downloads", jobId })`; seed
  analysis states from a CrateSet with `seedAnalysis(folder, set.tracks)`. Jobs stay live by themselves: `job`
  events update `item.text` and, when the text is a final status (downloaded/copied/exists/skipped/failed), also
  `item.status` ("cancelled" → `status: "failed", reason: "cancelled"`); `job_state` events update `job.state`
  (running → finalizing → done/cancelled) and refetch the job on finalizing; `job_done` replaces it. Use
  `useApp.getState().refreshJob(id)` (merges without rolling back SSE progress) instead of your own fetch+merge.
- **Loading**: `useAsync((signal) => api.crateSet(folder, { signal }), [folder, refreshKey])` → `{ data, error,
  loading, reload, setData }` (keeps old data while reloading; aborts stale requests).
- **Toasts**: `toast.success("…", { title, action: { label, onClick }, key, timeout })`.

## Audio (`@/audio/player`)

One global `<audio>`; never create your own. `player.togglePreview(track)` for row play buttons (plays the 20 s
clip; if not ready it prioritises the preview and starts when the `preview` event says ready; no_match → warning
toast). `player.toggleFile(id, setTrack.audio_url, { track, startAt })` for downloaded files. Also `play`,
`stop`, `pause`, `resume`, `seek(s)`, `seekFraction(f)`, `setVolume`. Reactive: `usePlayer((s) => s.position)`,
`useIsPlaying(id)`. **Space** plays/stops the current selection: on row click call
`useApp.getState().select({ kind: "preview", track })` (or `{ kind: "file", track, url }`).

## Routing, pages, keyboard

Hash routes: `#/search`, `#/crate/<seed_id>`, `#/downloads/<job_id>` (bare `#/downloads` → `jobId: ""` = job
list), `#/crates`, `#/set/<folder>`, `#/settings`, `#/ui`. `navigate({ name: "set", folder })`, `href(route)`, `<Link to={…}>`, `useRoute()`, `useHashQuery()`,
`setHashQuery({ q })` (replace, no history entry — e.g. the search term). Page skeleton:

```tsx
export function CratePage({ seedId }: { seedId: string }) {
  usePageTitle("Search", seed?.display_name);          // top-bar breadcrumb
  return (
    <Page>
      <PageHeader title=… subtitle=… actions={<Button variant="primary">Download 12</Button>}>{/* toolbar */}</PageHeader>
      <TableScroll className="flex-1">…</TableScroll>   {/* or <PageBody> for padded scrolling content */}
    </Page>
  );
}
```

Shortcuts: `useHotkey("Mod+Enter", fn, { enabled, allowInInputs })` (later registrations win; skipped while
typing and while a Modal is open). Global: Space play/stop selection, ⌘K focus search (from any page, even while
typing), ⌘. stop, ⌘1 Search, ⌘2 Crates, ⌘, Settings, ⌘[ / ⌘] back/forward. ⌘K uses `lib/focusSearch.ts`: the
search page calls `takePendingFocus()` on mount and listens for `FOCUS_SEARCH_EVENT` while mounted. `useListNav(ids, selectedId, onSelect)` gives ↑↓/j k/Home/End row navigation (rows need
`data-row-id`).

## Conventions

- Dense, dark, high contrast: 30 px table rows, 13 px text, 20 px page gutter (`px-5`; first/last table cells
  `pl-5`/`pr-5`). One primary button per screen. Numbers in `num`. Keys always via `KeyBadge`/`keyColor`, moves
  via `MoveChip`/`ScoreChip`/`moveColor`. Rule-failing rows use `Tr dim`. Use lucide icons at 14–16 px.
- Body text isn't selectable (desktop feel) — add `select-text` to paths, notes, error messages.
- No `localStorage` for server state; it's fine for view prefs (`seedbox.<page>.<pref>` keys, wrapped in try/catch).
- Errors: let the client toast, or pass `quiet` and render an `EmptyState`/inline message.
- Keep `deno task typecheck`, `lint` and `test` clean; check your screens at 1440×900 and 1180×760 (`deno task screenshot`).

## Mock mode (`VITE_MOCK=1`)

`src/mock/server.ts` implements every endpoint in memory (resets on reload) with 60–180 ms latency and SSE via
`MockEventSource`. Data: the real tunebat captures (seed **FISHER, Tones And I – Favour** `44wR4NqR75eJMETxrz59Sr`,
13 `r` recs, 50 search results), plus a few FISHER tracks as extra recs so there are rule-failing rows. Everything
the engine computes is replayed from `fixtures/engine.json` (`mock/engine.ts`), which `scripts/engine-fixtures.py`
writes by running musicreco on synthetic analyses of those tracks: rec scores and rule reasons, the crates' running
orders and energy ratings, transitions, the set plan and mix plans. The mock never scores or plans anything itself.
- Search: `fisher`, `favour`, `fisher favour` → the captured page (page 2 = the recs); other terms match
  title/artist/album (`chris lake`, `prospa`); a 22-char ID/URL returns that track; nonsense → empty page.
- Settings include `pretick_all: true` (all recs `ticked_default`; toggle it in Settings → only rule passes).
  `POST /api/system/pick-folder` simulates the dialog (returns `~/Music/DJ Crates`).
- `newCrate(id)` works for any fixture track; preloads previews (queued → loading → ready with peaks; some
  `no_match`/`error`); 7 crates in the library incl. one without metadata (`seed: null`).
- Jobs: 2 workers step through searching/downloading %/genre/loudness/tagging; `exists` for files already in the
  crate, `copied` for tracks in other crates, occasional `skipped`/`failed`; `job_state` finalizing/done (or cancelled), then `job_done`, analysis events
  and `set_changed`. A second job for the same crate waits; cancel/409/422 behave like the real server.
- Sets/analysis: synthetic `TrackAnalysis` (beat grid from tunebat BPM, 8/16/32-bar intros/outros, sections, cues;
  the 3-band waveform is drawn from the per-bar features at 50 pts/s) and the engine's `Transition`s; some tracks
  start `queued`/`running` and turn ready a few seconds after you open the set; one `error`.
- Fixture coverage: every pair of tracks within a crate has a transition; a crate in its own running order gets
  the set plan's transitions, a re-ordered one each pair's own (no set-level picks). Mix plans exist for the pairs
  of each crate's running order, for every technique at its own length, and in the first crate (Favour) at every
  length; another length falls back to the technique's own, another pair answers 409. Auto-order restores the
  engine's order. A pair a download adds shows the placeholder transition.
- Audio: synthesised WAV (kick/hat/bass at the track's BPM and key), so play/seek/progress work.

## Contract notes (for the lead)

- Resolved in types.ts: `job_state` event, `/api/covers/...` cover URLs, `PickFolderResult`, `UpdateResult`,
  `OkResponse`, `Settings.pretick_all`. The client still exports `YtdlpUpdateResult`/`OkResult` as aliases.

## Seedbox 2 shell and commands

The window is the icon rail (`Rail`: Library ⌘1, Crates ⌘2, Sets ⌘3, Discover ⌘4, Settings ⌘, at the bottom; the
logo goes home), the top bar (`TopBar`: back/forward, where you are, the ⌘K button, the engine connection, the
shortcuts button), the page in `main`, and the transport strip along the bottom (`TransportStrip`: play/pause,
stop, what plays with a seek bar, volume, from `usePlayer`; pass `title`/`subtitle` to `player.play`). Routes:
`#/`, `#/library`, `#/crates`, `#/sets`, `#/discover`, `#/mixes`, `#/settings` (`#/system` still works), `#/ui`,
`#/bench`.

**Every action is a command** (`lib/commands.ts`): the palette (⌘K, `CommandPalette`) runs it, `GlobalHotkeys`
binds its `combo`, and the shortcuts sheet (`?`, `ShortcutsSheet`) lists it. Don't bind an action with a bare
`useHotkey`; register it:

```tsx
useRegisterCommands([
  { id: "set.save", title: "Save set", group: "Actions", icon: Save, combo: "Mod+S", run: save },
], [save]);
```

Groups: Go to, Actions, Playback, View, App. `keywords` are extra words the palette matches by prefix, `hint`
shows a note ("Soon"), `enabled()` hides it while it can't run, `allowInInputs`/`allowInModal` let the shortcut
fire while typing or over a dialog, `altCombos` binds more keys without listing them. The shell's commands are in
`components/shell/appCommands.ts`. Things to go to (tracks, crates, sets) come from palette sources, registered by
the area that owns them: `registerPaletteSource({ id: "tracks", group: "Tracks", search: (q) => items })`; the
palette ranks what `search` returns with `lib/fuzzy.ts` (words in any order, word starts and runs score high).

## Tests (frontend2)

- **Unit and component tests:** `deno task test` (vitest). Unit tests run in node; component tests (Testing Library)
  start with `// @vitest-environment jsdom`, as in `src/components/ui/*.test.tsx`: query by role and accessible name,
  drive them with `userEvent`, and stub only the browser APIs jsdom lacks (`showModal`, `ResizeObserver`, layout).
  An `it.skip` there marks a known component bug.
- **Screenshot tests:** `deno task screenshot` builds `dist/`, then runs `scripts/screenshot.ts` in the Docker image of
  `scripts/screenshot.Dockerfile` (a pinned Chrome, Inter and JetBrains Mono, so a laptop and CI render the same
  pixels). It serves `dist/` with a stub `/api` (a working engine with a small library, one that never answers, or
  none; a shot can override any GET response), shoots every route and state in its `SHOTS` list at 1440×900 and
  1280×720 with animations off, and compares each with `visual-baselines/`. A screenshot fails when more than 16 of
  its pixels moved by more than 24/255: it renders the same pixels every time, and a changed word is about 70. The
  new screenshots go to `screenshots/actual/`, and for each failure `expected/` and `diff/` (changed pixels in red);
  CI uploads that folder as an artifact. When the change is intended, `UPDATE_SCREENSHOTS=1 deno task screenshot`
  rewrites the baselines (commit them with the change). `--only=home,settings` shoots a few, `--no-build` reuses
  `dist/`, `--no-docker` uses this machine's Chrome (quick to look at, but it won't match the baselines). A new
  screen or state adds a line to `SHOTS` (states: empty, loading, analysing, error).
