/**
 * The first run (#80), in place of the app: where the music is (or a track from tunebat to try it with), the models
 * with their sizes and progress, and what analysis does. Everything can be skipped or changed later in Settings.
 */
import { ArrowLeft, ArrowRight, AudioLines, Check, CloudDownload, HardDrive, Layers, Lock, Search, type LucideIcon } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { api } from "@/api/client";
import { Logo } from "@/components/shell/Logo";
import { Toaster } from "@/components/shell/Toaster";
import { Button, Panel, PanelHeader, WaveArt } from "@/components/ui";
import { DropOverlay } from "@/features/import/DropOverlay";
import { WatchFoldersPanel } from "@/features/import/WatchFoldersPanel";
import { ModelRow, ModelRowSkeleton } from "@/features/models/ModelsPanel";
import { cn } from "@/lib/cn";
import { fmtBytes } from "@/lib/format";
import { useModels } from "@/store/models";
import { finishOnboarding } from "@/store/onboarding";
import { TunebatTry } from "./TunebatTry";

const STEPS = ["Your music", "Models", "Analysis"] as const;

function Steps({ step }: { step: number }) {
  return (
    <ol className="flex items-center gap-2 text-xs" aria-label="Setup steps">
      {STEPS.map((name, i) => (
        <li key={name} className="flex items-center gap-2" aria-current={i === step ? "step" : undefined}>
          {i > 0 && <span className={cn("h-px w-6", i <= step ? "bg-accent" : "bg-line-strong")} />}
          <span className={cn("grid size-4.5 place-items-center rounded-full text-2xs font-semibold", i < step ? "bg-accent/25 text-accent-hover" : i === step ? "bg-accent text-accent-fg" : "bg-raised text-fg-subtle")}>
            {i < step ? <Check size={11} /> : i + 1}
          </span>
          <span className={i === step ? "font-medium text-fg" : "text-fg-subtle"}>{name}</span>
        </li>
      ))}
    </ol>
  );
}

function Heading({ title, text }: { title: string; text: string }) {
  return (
    <div>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-1 max-w-xl text-base text-fg-muted">{text}</p>
    </div>
  );
}

function Nav({ onBack, children }: { onBack?: () => void; children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 pt-1">
      {onBack && (
        <Button size="md" variant="ghost" icon={ArrowLeft} onClick={onBack}>
          Back
        </Button>
      )}
      <span className="flex-1" />
      {children}
    </div>
  );
}

function MusicStep({ onNext }: { onNext: () => void }) {
  const [trying, setTrying] = useState(false);
  return (
    <>
      <Heading title="Where is your music?" text="Seedbox 2 scans these folders now and follows them from then on. Your files stay where they are, and you can also drop files or folders onto this window." />
      <WatchFoldersPanel />
      <Panel className="max-w-3xl">
        <div className="flex items-center gap-3 px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-fg">No music to hand?</div>
            <div className="text-xs text-fg-subtle">Search tunebat and add a track or two to try Seedbox 2 with.</div>
          </div>
          {!trying && (
            <Button icon={Search} onClick={() => setTrying(true)}>
              Try with a search
            </Button>
          )}
        </div>
        {trying && <TunebatTry />}
      </Panel>
      <Nav>
        <Button size="md" variant="primary" iconRight={ArrowRight} autoFocus={!trying} onClick={onNext}>
          Continue
        </Button>
      </Nav>
    </>
  );
}

function ModelsStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const status = useModels((s) => s.status);
  useEffect(() => void useModels.getState().refresh(), []);
  const models = status?.models ?? [];
  const waiting = models.filter((m) => !m.installed && m.state !== "downloading");
  const downloading = models.some((m) => m.state === "downloading");
  const size = waiting.reduce((sum, m) => sum + m.size - m.done, 0);
  const startAll = () => void api.models.downloadMissing().then((s) => useModels.getState().set(s), () => {});
  return (
    <>
      <Heading title="Download the models" text="They make the analysis better, and run on this computer. Seedbox 2 works without them, on simpler methods, so you can skip this and download them later in Settings." />
      <Panel className="max-w-3xl">
        <PanelHeader title="Models" description={status ? `${fmtBytes(status.total_bytes)} in all, ${fmtBytes(status.installed_bytes)} here so far` : "Checking what is here…"} />
        {status ? models.map((m) => <ModelRow key={m.name} m={m} />) : <ModelRowSkeleton />}
      </Panel>
      <Nav onBack={onBack}>
        {waiting.length > 0 && !downloading ? (
          <>
            <Button size="md" onClick={onNext}>
              Skip for now
            </Button>
            <Button size="md" variant="primary" icon={CloudDownload} autoFocus onClick={startAll}>
              Download {fmtBytes(size)}
            </Button>
          </>
        ) : (
          <Button size="md" variant="primary" iconRight={ArrowRight} autoFocus onClick={onNext}>
            {downloading ? "Continue while they download" : "Continue"}
          </Button>
        )}
      </Nav>
    </>
  );
}

const ANALYSIS: { icon: LucideIcon; title: string; text: string }[] = [
  { icon: AudioLines, title: "It listens to every track", text: "The waveform comes first, in a moment. Tempo, key and energy follow; until then you see what the file's tags say, in grey." },
  { icon: Layers, title: "Stems take longest", text: "Splitting drums, bass, vocals and the rest takes the most time, so it runs in the background, one track at a time." },
  { icon: HardDrive, title: "It happens once", text: "Results are kept by the sound of the track, so renaming, retagging or moving a file keeps them. After a restart, it carries on." },
  { icon: Lock, title: "It stays on this computer", text: "Your music is analysed here and never uploaded. You can keep working: the progress shows in the top bar." },
];

function AnalysisStep({ onBack }: { onBack: () => void }) {
  return (
    <>
      <Heading title="What analysis does" text="Seedbox 2 mixes on what it hears in the music, so it needs to listen first. You don't have to do anything." />
      <Panel className="max-w-3xl">
        {ANALYSIS.map(({ icon: Icon, title, text }) => (
          <div key={title} className="flex gap-3 border-b border-line/60 px-4 py-3 last:border-b-0">
            <Icon size={18} className="mt-0.5 shrink-0 text-accent-hover" />
            <div>
              <div className="text-sm font-medium text-fg">{title}</div>
              <p className="mt-0.5 text-sm text-fg-muted">{text}</p>
            </div>
          </div>
        ))}
      </Panel>
      <Nav onBack={onBack}>
        <Button size="md" variant="primary" iconRight={ArrowRight} autoFocus onClick={() => void finishOnboarding()}>
          Open my library
        </Button>
      </Nav>
    </>
  );
}

export function Onboarding() {
  const [step, setStep] = useState(0);
  return (
    <div className="flex h-full flex-col bg-surface">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-panel px-5">
        <Logo />
        <span className="text-base font-semibold">Welcome to Seedbox 2</span>
        <span className="flex-1" />
        <Steps step={step} />
        <span className="flex-1" />
        <Button variant="ghost" onClick={() => void finishOnboarding()}>
          Skip setup
        </Button>
      </header>
      <main className="flex min-h-0 flex-1 flex-col overflow-auto scroll-thin">
        <div key={step} className="mx-auto flex w-full max-w-3xl shrink-0 animate-route-in flex-col gap-4 px-5 pt-10 pb-6">
          {step === 0 && <MusicStep onNext={() => setStep(1)} />}
          {step === 1 && <ModelsStep onBack={() => setStep(0)} onNext={() => setStep(2)} />}
          {step === 2 && <AnalysisStep onBack={() => setStep(1)} />}
        </div>
        <WaveArt seed={step + 3} className="mt-auto h-28 w-full max-w-none shrink-0 opacity-40" />
      </main>
      <Toaster />
      <DropOverlay />
    </div>
  );
}
