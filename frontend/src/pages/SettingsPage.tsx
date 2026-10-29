import { CircleArrowUp, FolderOpen, FolderSearch, HardDrive, RotateCcw, Save } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { MixLength, MixView, Settings } from "@/api/types";
import { MIX_VIEWS } from "@/features/set/mix/views";
import { usePageTitle } from "@/components/shell/pageTitle";
import { Badge, Button, Field, InfoRow, Input, Kbd, Page, PageBody, PageHeader, Panel, PanelHeader, ProgressBar, Slider, Spinner, Switch, Tabs } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtMb, tildify } from "@/lib/format";
import { useHotkey } from "@/lib/hotkeys";
import { useApp } from "@/store/app";
import { runYtdlpUpdate, useYtdlpPendingRestart, useYtdlpUpdate, ytdlpOutdated } from "@/store/system";
import { toast } from "@/store/toasts";

const DEFAULTS: Settings = {
  output_root: "~/Music/MusicReco",
  workers: 2,
  tolerance_s: 15,
  bpm_tolerance: 0.1,
  preview_concurrency: 3,
  preview_cache_mb: 500,
  pretick_all: true,
  mix_length: "normal",
  mix_view: "simple",
};

const MIX_LENGTHS: { id: MixLength; label: string; hint: string }[] = [
  { id: "short", label: "Short", hint: "8-bar blends: quick mixing, more tracks per hour." },
  { id: "normal", label: "Normal", hint: "16-bar blends, 32 when both edges are long drum intros/outros. 8 only when the edges are too short or the vocals clash." },
  { id: "long", label: "Long", hint: "32-bar blends wherever a 32-bar intro or outro leaves room, else 16: A's outro rides under B's intro for a full phrase before A goes." },
];

const CACHE_STEPS = [100, 250, 500, 1000, 2000, 5000];

const SHORTCUTS: [string, string][] = [
  ["Space", "Play / stop the selected track's preview"],
  ["Mod+k", "Focus search (from any page)"],
  ["Mod+.", "Stop playback"],
  ["Mod+1", "Search"],
  ["Mod+2", "Crates"],
  ["Mod+,", "Settings"],
  ["Mod+[", "Back"],
  ["Mod+]", "Forward"],
];

function diff(a: Settings, b: Settings): Partial<Settings> {
  const out: Partial<Settings> = {};
  for (const k of Object.keys(b) as (keyof Settings)[]) if (a[k] !== b[k]) (out as Record<string, unknown>)[k] = b[k];
  return out;
}

function SliderField({
  label,
  value,
  display,
  hint,
  min,
  max,
  step,
  onChange,
  isDefault,
  minLabel,
  maxLabel,
}: {
  label: string;
  value: number;
  display: ReactNode;
  hint: ReactNode;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  isDefault: boolean;
  minLabel?: string;
  maxLabel?: string;
}) {
  return (
    <Field
      label={label}
      hint={hint}
      aside={
        <span className="inline-flex items-center gap-1.5">
          <span className="font-semibold text-fg">{display}</span>
          {isDefault && <span className="text-fg-faint">default</span>}
        </span>
      }
    >
      <div className="flex items-center gap-3 py-0.5">
        <span className="min-w-8 text-right text-2xs whitespace-nowrap text-fg-faint num">{minLabel ?? min}</span>
        <Slider value={value} min={min} max={max} step={step} onChange={onChange} label={label} />
        <span className="min-w-8 text-2xs whitespace-nowrap text-fg-faint num">{maxLabel ?? max}</span>
      </div>
    </Field>
  );
}

function SystemPanel() {
  const system = useApp((s) => s.system);
  const { running, result, error } = useYtdlpUpdate();
  const pending = useYtdlpPendingRestart(system?.ytdlp_version);
  const outdated = system ? ytdlpOutdated(system.ytdlp_version, system.ytdlp_latest) : false;
  return (
    <Panel>
      <PanelHeader title="System" description="Bundled tools the engine uses" />
      {!system ? (
        <div className="flex items-center gap-2 px-4 py-4 text-sm text-fg-subtle">
          <Spinner /> Loading system status…
        </div>
      ) : (
        <div>
          <InfoRow label="Seedbox">
            <span className="num">v{system.version}</span>
          </InfoRow>
          <InfoRow label="yt-dlp">
            <span className="font-medium num">{system.ytdlp_version}</span>
            {pending ? (
              <Badge tone="ok" size="xs">
                {pending} installed · restart to apply
              </Badge>
            ) : outdated ? (
              <Badge tone="warn" size="xs">
                {system.ytdlp_latest} available
              </Badge>
            ) : (
              <span className="text-xs text-fg-subtle">no update offered</span>
            )}
          </InfoRow>
          <div className="border-b border-line/60 px-4 py-3">
            <p className="mb-2.5 text-xs text-fg-subtle">YouTube changes often — update first when downloads or previews fail. Updates apply the next time Seedbox starts.</p>
            <div className="flex items-center gap-3">
              <Button variant={outdated ? "primary" : "secondary"} icon={CircleArrowUp} loading={running} onClick={() => void runYtdlpUpdate()}>
                {running ? "Updating yt-dlp…" : outdated ? `Update to ${system.ytdlp_latest}` : "Check for update"}
              </Button>
            </div>
            {running && <ProgressBar className="mt-3" value={null} size="xs" label="Updating yt-dlp" />}
            {!running && result && (
              <p className={cn("mt-2.5 text-xs select-text", result.ok ? "text-ok" : "text-warn")}>
                {result.ok ? "✓ " : ""}
                {result.message}
                {result.ok && pending ? " — restart Seedbox to start using it." : ""}
              </p>
            )}
            {!running && error && <p className="mt-2.5 text-xs text-danger select-text">{error}</p>}
          </div>
          <InfoRow label="ffmpeg">
            {system.ffmpeg ? (
              <>
                <Badge tone="ok" size="xs">
                  found
                </Badge>
                <span className="truncate font-mono text-xs text-fg-muted select-text" title={system.ffmpeg}>
                  {tildify(system.ffmpeg)}
                </span>
              </>
            ) : (
              <Badge tone="danger" size="xs">
                missing — downloads can't encode MP3
              </Badge>
            )}
          </InfoRow>
          <InfoRow label="deno">
            {system.deno ? (
              <>
                <Badge tone="ok" size="xs">
                  found
                </Badge>
                <span className="truncate font-mono text-xs text-fg-muted select-text" title={system.deno}>
                  {tildify(system.deno)}
                </span>
              </>
            ) : (
              <Badge tone="warn" size="xs">
                missing — some YouTube videos may fail
              </Badge>
            )}
          </InfoRow>
          <InfoRow label="Crates folder">
            <span className="truncate font-mono text-xs text-fg-muted select-text" title={system.output_root}>
              {tildify(system.output_root)}
            </span>
          </InfoRow>
        </div>
      )}
    </Panel>
  );
}

function ShortcutsPanel() {
  return (
    <Panel>
      <PanelHeader title="Keyboard" />
      <div className="py-1">
        {SHORTCUTS.map(([combo, what]) => (
          <div key={combo} className="flex h-8 items-center justify-between gap-3 px-4 text-sm">
            <span className="text-fg-muted">{what}</span>
            <Kbd combo={combo} subtle />
          </div>
        ))}
      </div>
    </Panel>
  );
}

export function SettingsPage() {
  usePageTitle("Settings");
  const saved = useApp((s) => s.settings);
  const setSettings = useApp((s) => s.setSettings);
  const [form, setForm] = useState<Settings | null>(saved);
  const [saving, setSaving] = useState(false);
  const [pickerAvailable, setPickerAvailable] = useState(true); // false after a 501 (browser / dev: no native window)
  const [picking, setPicking] = useState(false);

  // adopt server settings when they arrive / change (unless the user is mid-edit)
  useEffect(() => {
    setForm((f) => (f === null || (saved && Object.keys(diff(saved, f)).length === 0) ? saved : f));
  }, [saved]);

  const changes = useMemo(() => (saved && form ? diff(saved, form) : {}), [saved, form]);
  const dirty = Object.keys(changes).length > 0;
  const rootError = form && !/^(\/|~)/.test(form.output_root.trim()) ? "Use an absolute path (starting with / or ~)" : null;

  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setForm((f) => (f ? { ...f, [k]: v } : f));

  const chooseFolder = async () => {
    if (!form) return;
    setPicking(true);
    try {
      const { path } = await api.pickFolder(form.output_root, { quiet: true });
      if (path) set("output_root", path);
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) setPickerAvailable(false);
      else toast.error((e as Error).message, { title: "Folder dialog" });
    } finally {
      setPicking(false);
    }
  };

  const save = async () => {
    if (!form || !dirty || rootError) return;
    setSaving(true);
    try {
      const patch = { ...changes, ...(changes.output_root ? { output_root: changes.output_root.trim() } : {}) };
      const next = await api.saveSettings(patch);
      setSettings(next);
      setForm(next);
      toast.success("Settings saved");
      if (patch.output_root) {
        void useApp.getState().refreshBasics();
      }
    } catch {
      /* toast shown by the client */
    } finally {
      setSaving(false);
    }
  };

  useHotkey("Mod+s", () => void save(), { allowInInputs: true, enabled: dirty });

  const cacheIdx = form ? CACHE_STEPS.reduce((best, v, i) => (Math.abs(v - form.preview_cache_mb) < Math.abs(CACHE_STEPS[best] - form.preview_cache_mb) ? i : best), 0) : 2;

  return (
    <Page>
      <PageHeader
        title="Settings"
        subtitle="Stored in ~/Library/Application Support/Seedbox/settings.json"
        actions={
          <>
            {dirty && <span className="text-xs text-warn">Unsaved changes</span>}
            <Button icon={RotateCcw} variant="ghost" disabled={!dirty || saving} onClick={() => setForm(saved)}>
              Revert
            </Button>
            <Button icon={Save} variant="primary" kbd="Mod+S" disabled={!dirty || !!rootError} loading={saving} onClick={() => void save()}>
              Save
            </Button>
          </>
        }
      />
      <PageBody className="px-5 py-5">
        {!form ? (
          <div className="flex items-center gap-2 text-sm text-fg-subtle">
            <Spinner /> Loading settings…
          </div>
        ) : (
          <div className="mx-auto flex max-w-[1180px] items-start gap-5">
            <div className="flex min-w-0 flex-1 flex-col gap-5">
              <Panel>
                <PanelHeader title="Library" description="Where crates are written" />
                <div className="flex flex-col gap-4 px-4 py-4">
                  <Field
                    label="Crates folder"
                    htmlFor="output_root"
                    error={rootError}
                    hint={
                      <>
                        Each seed gets a sub-folder: <span className="font-mono">{tildify(form.output_root) || "…"}/Artists - Title/</span>. Changing this
                        doesn't move existing crates.
                      </>
                    }
                  >
                    <div className="flex items-center gap-2">
                      <Input
                        id="output_root"
                        icon={HardDrive}
                        size="md"
                        wrapperClassName="flex-1"
                        className="font-mono text-sm"
                        spellCheck={false}
                        value={form.output_root}
                        invalid={!!rootError}
                        onChange={(e) => set("output_root", e.target.value)}
                      />
                      {pickerAvailable && (
                        <Button size="md" icon={FolderSearch} loading={picking} onClick={() => void chooseFolder()}>
                          Choose…
                        </Button>
                      )}
                      <Button size="md" icon={FolderOpen} onClick={() => void api.reveal(saved?.output_root ?? form.output_root)}>
                        Reveal in Finder
                      </Button>
                      <Button size="md" variant="ghost" disabled={tildify(form.output_root.trim()).replace(/\/+$/, "") === DEFAULTS.output_root} onClick={() => set("output_root", DEFAULTS.output_root)}>
                        Default
                      </Button>
                    </div>
                  </Field>
                </div>
              </Panel>

              <Panel>
                <PanelHeader title="Downloads" description="YouTube Music first, regular YouTube as fallback; MP3 192 kbps" />
                <div className="grid grid-cols-2 gap-x-8 gap-y-5 px-4 py-4">
                  <SliderField
                    label="Parallel downloads"
                    value={form.workers}
                    display={form.workers}
                    min={1}
                    max={6}
                    step={1}
                    onChange={(v) => set("workers", v)}
                    isDefault={form.workers === DEFAULTS.workers}
                    hint="Tracks downloaded at the same time. More than 3 risks YouTube rate limits."
                  />
                  <SliderField
                    label="Length tolerance"
                    value={form.tolerance_s}
                    display={`±${form.tolerance_s} s`}
                    min={5}
                    max={30}
                    step={1}
                    onChange={(v) => set("tolerance_s", v)}
                    minLabel="5 s"
                    maxLabel="30 s"
                    isDefault={form.tolerance_s === DEFAULTS.tolerance_s}
                    hint="A YouTube match must be this close to the Spotify length, otherwise the track is skipped (never guessed)."
                  />
                </div>
              </Panel>

              <Panel>
                <PanelHeader title="Mixing rule" description="Which recommendations start ticked" />
                <div className="flex items-start justify-between gap-6 border-b border-line/60 px-4 py-4">
                  <div className="min-w-0">
                    <label htmlFor="pretick_all" className="text-sm font-medium text-fg">
                      Pre-tick all recommendations
                    </label>
                    <p className="mt-1 text-xs text-fg-subtle">
                      On: every tunebat recommendation starts ticked. Off: only key-adjacent tracks within the BPM tolerance start ticked.
                    </p>
                  </div>
                  <span className="flex shrink-0 items-center gap-2 pt-0.5">
                    {form.pretick_all === DEFAULTS.pretick_all && <span className="text-xs text-fg-faint">default</span>}
                    <Switch id="pretick_all" checked={form.pretick_all} onChange={(v) => set("pretick_all", v)} />
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-x-8 gap-y-5 px-4 py-4">
                  <SliderField
                    label="BPM tolerance"
                    value={Math.round(form.bpm_tolerance * 100)}
                    display={`±${Math.round(form.bpm_tolerance * 100)}%`}
                    min={2}
                    max={20}
                    step={1}
                    onChange={(v) => set("bpm_tolerance", v / 100)}
                    minLabel="2%"
                    maxLabel="20%"
                    isDefault={Math.abs(form.bpm_tolerance - DEFAULTS.bpm_tolerance) < 1e-9}
                    hint={
                      form.pretick_all
                        ? "The mixing rule: tracks in this tempo range (half/double time counts) that are also key-adjacent pass (✓, ranked first)."
                        : "Pre-tick tracks within this tempo range of the seed (half/double time counts) that are also adjacent on the Camelot wheel."
                    }
                  />
                  <div className="rounded-sm border border-line bg-bg/40 px-3 py-2.5 text-xs leading-relaxed text-fg-subtle">
                    <div className="mb-1 font-medium text-fg-muted">Example at ±{Math.round(form.bpm_tolerance * 100)}%</div>
                    A 128 BPM seed {form.pretick_all ? "passes" : "pre-ticks"} tracks at{" "}
                    <span className="whitespace-nowrap text-fg num">
                      {Math.round(128 * (1 - form.bpm_tolerance))}–{Math.round(128 * (1 + form.bpm_tolerance))} BPM
                    </span>
                    , or{" "}
                    <span className="whitespace-nowrap text-fg num">
                      {Math.round(64 * (1 - form.bpm_tolerance))}–{Math.round(64 * (1 + form.bpm_tolerance))}
                    </span>{" "}
                    at half time. Key: same, ±1 or relative.
                  </div>
                </div>
              </Panel>

              <Panel>
                <PanelHeader title="Mixes" description="How the mix planner blends one track into the next" />
                <div className="flex items-start justify-between gap-6 px-4 py-4">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-fg">Mix length</div>
                    <p className="mt-1 text-xs text-fg-subtle">{MIX_LENGTHS.find((m) => m.id === form.mix_length)?.hint}</p>
                  </div>
                  <span className="flex shrink-0 items-center gap-2 pt-0.5">
                    {form.mix_length === DEFAULTS.mix_length && <span className="text-xs text-fg-faint">default</span>}
                    <Tabs variant="segmented" value={form.mix_length} onChange={(v) => set("mix_length", v)} items={MIX_LENGTHS.map(({ id, label }) => ({ id, label }))} />
                  </span>
                </div>
                <div className="flex items-start justify-between gap-6 border-t border-line/60 px-4 py-4">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-fg">Transition editor</div>
                    <p className="mt-1 text-xs text-fg-subtle">{MIX_VIEWS.find((m) => m.id === form.mix_view)?.hint}</p>
                  </div>
                  <span className="flex shrink-0 items-center gap-2 pt-0.5">
                    {form.mix_view === DEFAULTS.mix_view && <span className="text-xs text-fg-faint">default</span>}
                    <Tabs variant="segmented" value={form.mix_view} onChange={(v) => set("mix_view", v as MixView)} items={MIX_VIEWS.map(({ id, label }) => ({ id, label }))} />
                  </span>
                </div>
              </Panel>

              <Panel>
                <PanelHeader title="Previews" description="20 s clips preloaded when a crate opens" />
                <div className="grid grid-cols-2 gap-x-8 gap-y-5 px-4 py-4">
                  <SliderField
                    label="Clips loaded at once"
                    value={form.preview_concurrency}
                    display={form.preview_concurrency}
                    min={1}
                    max={6}
                    step={1}
                    onChange={(v) => set("preview_concurrency", v)}
                    isDefault={form.preview_concurrency === DEFAULTS.preview_concurrency}
                    hint="Background preloading in parallel. Ticked and high-score tracks go first."
                  />
                  <SliderField
                    label="Preview cache"
                    value={cacheIdx}
                    display={fmtMb(form.preview_cache_mb)}
                    min={0}
                    max={CACHE_STEPS.length - 1}
                    step={1}
                    onChange={(i) => set("preview_cache_mb", CACHE_STEPS[i])}
                    minLabel={fmtMb(CACHE_STEPS[0])}
                    maxLabel={fmtMb(CACHE_STEPS[CACHE_STEPS.length - 1])}
                    isDefault={form.preview_cache_mb === DEFAULTS.preview_cache_mb}
                    hint="Disk space for clips, shared across sessions (~0.3 MB each). Oldest clips are evicted first."
                  />
                </div>
              </Panel>
            </div>

            <div className="flex w-[400px] shrink-0 flex-col gap-5">
              <SystemPanel />
              <ShortcutsPanel />
            </div>
          </div>
        )}
      </PageBody>
    </Page>
  );
}
