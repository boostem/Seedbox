# Seedbox 2 design language (v2)

How Seedbox 2 looks and moves. The tokens live in `src/index.css` (`@theme static`); `#/ui` shows them, and
`src/design-tokens.test.ts` (part of `deno task test`) keeps the code on them. Components, data, routing and the
other conventions are in `DESIGN.md`.

The base is Seedbox 1's dark, dense look, which the owner likes ("ALMOST looks perfect"). v2 is the polish: one
scale for every size, motion with a meaning, music colours that don't double as warnings, and no text too small to
read.

## Principles

1. **One accent.** Indigo (`accent`) marks what you can act on or what is selected: the primary button, the
   selected row, focus, the active nav item. It is never a music colour or a status colour, and a screen has one
   primary button.
2. **Content first.** The music (waveforms, keys, sections, artwork) carries the colour. Chrome stays in greys:
   surfaces step from `bg` to `overlay`, text from `fg` to `fg-faint`. Borders are 1 px `line`; reach for a
   shadow only when something floats.
3. **Motion explains change.** Animate only to show where something came from or what changed: a panel slides in
   from its edge, a toast rises from the corner, a value bar grows. Nothing loops except progress. Every duration
   is a token, and `prefers-reduced-motion` turns them all into instant changes.
4. **Colour has one meaning.** Status colours say how the app is doing; music colours say what is in the track. A
   drop is not an error, so it is never red.
5. **Readable at arm's length.** 13 px base, nothing under 10 px (canvas and SVG included), tabular numbers for
   anything that counts (`num`).
6. **On the scale.** No arbitrary values (`h-[18px]`, `z-[1000]`, `#fff` in a component). If a size is missing,
   add a token here and in `index.css`, with its role.

## Type

System font (SF Pro in the macOS webview, Segoe UI on Windows), `font-mono` for paths and token names.

| Token | Size / line | Use |
|---|---|---|
| `text-2xs` | 10 / 14 | the floor: `label-caps` section labels, canvas labels, kbd hints |
| `text-xs` | 11 / 15 | meta, badges, table headers, tooltips |
| `text-sm` | 12 / 16 | secondary text, small buttons and inputs |
| `text-base` | 13 / 18 | body, table cells, default buttons |
| `text-md` | 14 / 20 | large buttons, emphasised values |
| `text-lg` | 16 / 22 | panel titles in dialogs |
| `text-xl` | 20 / 26 | page titles |
| `text-2xl` | 26 / 32 | hero numbers (BPM, key) |

- Weights: 400 body, 500 buttons and labels, 600 titles and `label-caps`, 700 only for numbers on canvas.
- `label-caps`: 10 px, uppercase, `tracking-caps` (0.08em), `fg-subtle`.
- Canvas and SVG: `textPx(n)` from `lib/theme.ts` clamps a scaled size to `MIN_TEXT_PX` (10). Canvas labels are
  10 px; label boxes are 13 px high.

## Spacing and sizes

- **Spacing** is Tailwind's 4 px grid: `1` = 4 px, `2` = 8, `3` = 12, `4` = 16, `5` = 20. Half steps
  (`h-4.5` = 18 px, `w-0.75` = 3 px) are on the scale; bracketed pixels are not.
- Page gutter 20 px (`px-5`), panel padding 16 px (`p-4`), gaps 8–16 px, table rows 30 px.
- **Control heights**: every control that sits in a row with others has one of four heights, so they line up.

| Token | Height | Use |
|---|---|---|
| `h-control-sm` | 24 | `Button size="xs"`, small tabs, icon buttons in toolbars |
| `h-control-md` | 28 | the default: `Button size="sm"`, `Input size="sm"`, `Select`, tabs |
| `h-control-lg` | 32 | `Button size="md"`, `Input size="md"`, search fields |
| `h-control-xl` | 40 | `Button size="lg"`, transport, the main call to action |

  `size-control-*` gives square icon buttons of the same heights. Chips sit below the scale on purpose: badges and
  `Kbd` are 16 or 18 px (`h-4`, `h-4.5`), dense in-row icon buttons 20 px (`size-5`).
- **Layout sizes**: `w-rail` (56 px, the icon rail), `h-transport` (56 px, the transport strip), `w-toast` (360 px), and Tailwind's container scale for the rest:
  `max-w-xs` tooltips (320), `max-w-md` empty-state text (448), `max-w-3xl` settings panels (768), `max-w-4xl`
  dashboards (896); modals `w-sm` / `w-lg` / `w-3xl` / `w-5xl`; the command palette `w-xl`, the shortcuts sheet
  `w-md`.
- **Window sizes**: from 1280×720 to 2560×1440. Nothing in the shell clips at 1280×720; pages scroll inside `main`.

## Radii

`rounded-xs` 2 (chips, kbd), `rounded-sm` 3 (buttons, inputs, tooltips), `rounded-md` 5 (large buttons, toasts),
`rounded-lg` 8 (panels, dialogs), `rounded-xl` 12 (artwork, hero cards), `rounded-full` (dots, switches).

## Elevation and layers

Elevation goes with the layer: the higher an element sits, the stronger its shadow and its `z-*`.

| Level | Shadow | Layer | What |
|---|---|---|---|
| 0 | none, 1 px `line` | — | page content, panels, tables |
| 1 | `shadow-card` | — | cards, raised tiles |
| — | — | `z-sticky` (10) | sticky table headers, pinned toolbars |
| 2 | `shadow-pop` | `z-dropdown` (100) | menus, popovers, floating labels over the waveform |
| 3 | `shadow-modal` | `z-modal` (200) | dialogs (native `<dialog>` is in the top layer anyway) |
| 2 | `shadow-pop` | `z-toast` (300) | toasts |
| 2 | `shadow-pop` | `z-tooltip` (400) | tooltips, above everything |

Small shadows with a role: `shadow-hairline` (the inner edge of artwork), `shadow-highlight` (the light top edge
of a filled button), `shadow-knob` (switch knob), `shadow-selected-edge` (the accent bar on a selected row).

## Motion

| Token | Value | Use |
|---|---|---|
| `duration-fast` | 120 ms | state: hover, press, toggle, focus. The default for every bare `transition-*` |
| `duration-base` | 200 ms | things appearing: toasts, tooltips, menus, artwork fading in |
| `duration-slow` | 320 ms | layout: panels, drawers, progress bars, expanding rows |
| `ease-standard` | `cubic-bezier(0.2, 0, 0, 1)` | the default: something moving on screen |
| `ease-enter` | `cubic-bezier(0, 0, 0.2, 1)` | something arriving (decelerates) |
| `ease-exit` | `cubic-bezier(0.4, 0, 1, 1)` | something leaving (accelerates); pair with `duration-fast` |
| `ease-spring` | `linear(…)`, ζ ≈ 0.72 | panels and drawers sliding in, with `duration-slow`: a 4 % overshoot that settles |

- `transition-colors` alone is enough for hover states (120 ms, `ease-standard`).
- Animate `transform` and `opacity`; `width` only for progress bars. Never animate the waveform canvas itself:
  it redraws at frame rate.
- Exits are faster than entrances. Nothing animates on first paint of a page.
- The animations, from `index.css`:

| Utility | What |
|---|---|
| `animate-route-in` | the page after a route change rises 4 px and fades in (`base`, `enter`); not on launch |
| `animate-pop-in` / `animate-pop-out` | the command palette drops in from above (`base`, `enter`) and leaves (`fast`, `exit`) |
| `animate-panel-in` / `animate-panel-out` | a side panel (the shortcuts sheet) slides in from its edge (`slow`, `spring`) and out (`fast`, `exit`) |
| `animate-fade-in` / `animate-fade-out` | backdrops of those dialogs, small markers |
| `animate-toast-in` | a toast rises from the corner |

  The rail's accent bar slides to the active area with `transition-transform duration-slow ease-spring`.
- To animate something out, keep it mounted until its exit animation ends: `usePresence(open)` (`lib/usePresence.ts`)
  gives `mounted`, a `data-state` of `open`/`closed` for the CSS and the `onAnimationEnd` handler.
- `prefers-reduced-motion: reduce` makes every transition and animation instant (`index.css`).

## Colour roles

Tailwind's own palette is removed: only these exist, plus `white` and `black`. Opacity modifiers work
(`bg-danger/12`). For `<canvas>`, `wavePalette()` (`features/waveform/palette.ts`) reads the same tokens with
`cssVar()`; it has no fallbacks, so a colour lives in `index.css` and nowhere else.

### Surfaces and text

| Token | Use |
|---|---|
| `bg` | window, gaps between panels, behind waveforms |
| `panel` | icon rail, top bar, transport strip, table header |
| `surface` | page content, cards, table body |
| `raised` | inputs, secondary buttons, hovered controls |
| `overlay` | menus, dialogs, tooltips, toasts |
| `hover`, `selected` | row hover; selected row (accent-tinted) |
| `line`, `line-strong` | 1 px dividers; input borders and emphasis |
| `fg`, `fg-muted`, `fg-subtle`, `fg-faint` | primary text; secondary; labels and meta; disabled and hints |

### Accent

`accent` (indigo `#6f7dff`), `accent-hover`, `accent-soft` (selection, tinted backgrounds), `accent-fg` (text on
accent), `focus` (the focus ring). `brand-deep` is the logo gradient's dark end and is used nowhere else.

### Status: how the app is doing

`ok`, `warn`, `danger`, `info`. Jobs, connection, health checks, toasts, validation, destructive buttons, and real
problems in a mix (a vocal clash). Never on a section, cue, stem or key.

### Music: what is in the track

Music colours never mean good or bad. They are mid-saturation so the 3-band waveform stays the brightest thing on
screen.

| Group | Tokens | Notes |
|---|---|---|
| Sections | `section-intro` / `section-outro` teal, `section-build` violet, `section-drop` rose, `section-breakdown` sky, `section-body` grey | bands under the waveform; intro and outro share a colour (they are where mixes happen) |
| Phrases | `phrase` | the phrase lines and their bar numbers, heavier for a 16- and a 32-bar phrase than an 8; bar lines are `fg` at low alpha. Phrase bands behind the waveform take their section's colour |
| Cues | `cue-mix` lime; `cue-1` … `cue-8` coral, orange, yellow, green, teal, blue, violet, pink | mix-in and mix-out cues; a hot cue takes its slot's colour (A-H) with its letter on it; every other cue takes its section's colour (`cueColor`) |
| Transition styles | `style-blend` sky, `style-swap` blue, `style-filter` lavender, `style-echo` orchid, `style-cut` coral | the overlap region of a plan on a deck and in the transition editor, by its technique (`styleColor`) |
| Stems | `stem-drums` yellow, `stem-bass` blue, `stem-vocals` magenta, `stem-other` green | stem lanes, and intro/outro brackets by what plays in them (drums only, vocals, music) |
| Waveform | `wave-low` blue, `wave-mid` amber, `wave-high` white, `wave-played`, `playhead` | Rekordbox-style 3 bands |
| RGB waveform | `wave-rgb-low` red, `wave-rgb-mid` green, `wave-rgb-high` blue | the RGB mode (Settings): each column mixes them by its band balance |
| Keys | the 24 Camelot colours, `key-unknown`, `on-key` | below |
| Moves | `move-same`, `move-step`, `move-relative`, `move-boost`, `move-clash`, `move-unknown` | how two keys relate (Seedbox 1's README, "Score and colours") |

### Camelot

`lib/camelot.ts` computes the 24 key colours: 12 hues around the wheel in Mixed In Key's order (1 teal … 4 yellow
… 7 red … 10 violet … 12 sky), A (minor) deeper, B (major) lighter, with yellows and greens toned down so every key
reads at the same weight. `keyColor(code)` gives `{ base, bg, border, solidFg }`; `--camelot-<code>` and
`--camelot-<code>-bg` are on `:root` for CSS. An unknown key is `key-unknown`; text on a filled key colour is
`on-key`. Never hard-code a key colour.

## Rules the test enforces

`src/design-tokens.test.ts` fails on:
- a hex colour in any `.ts`/`.tsx` file (comments aside): colours are tokens in `index.css`;
- an arbitrary value with a unit, colour or timing (`h-[18px]`, `shadow-[0_1px_…]`, `text-[9px]`), or any arbitrary
  `z-`, `duration-`, `ease-`, `leading-`, `tracking-`, `text-`, `shadow-` or colour utility. Viewport and percent
  sizes (`max-h-[85vh]`), keywords (`rounded-[inherit]`) and property lists (`transition-[width]`) are allowed;
- numeric `z-10`, `duration-150`, `delay-75`: use `z-sticky` … `z-tooltip` and `duration-fast/base/slow`;
- a `--text-*` token, a canvas font (`` `600 9px ${…}` ``, `font(p, 9)`) or an SVG `fontSize` under 10 px.
