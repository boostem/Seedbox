/** Crate page header: the seed (cover, key, BPM, energy, links) + its own preview. */
import { ExternalLink, FolderOpen, Layers, Sprout } from "lucide-react";
import type { ReactNode } from "react";
import { api } from "@/api/client";
import type { Crate, CrateSummary } from "@/api/types";
import { Button, CoverArt, KeyBadge, Tooltip } from "@/components/ui";
import { cn } from "@/lib/cn";
import { fmtBpm, fmtTime, plural } from "@/lib/format";
import { navigate } from "@/lib/router";
import { PreviewButton, PreviewWave } from "./PreviewControls";
import { fmtReleaseDate, pct01, tunebatUrl } from "./trackMeta";

function Stat({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <span className="label-caps">{label}</span>
      <span className="flex h-6 items-center gap-1.5 text-md font-medium num">{children}</span>
    </div>
  );
}

function Meter({ value }: { value: number }) {
  return (
    <span className="relative h-1 w-10 overflow-hidden rounded-full bg-line-strong/70">
      <span className="absolute inset-y-0 left-0 rounded-full bg-fg-muted" style={{ width: `${Math.round(value * 100)}%` }} />
    </span>
  );
}

function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      draggable={false}
      className="inline-flex h-7 items-center gap-1.5 rounded-sm px-2 text-sm text-fg-muted transition-colors hover:bg-raised hover:text-fg"
    >
      {children}
      <ExternalLink size={11} className="opacity-60" />
    </a>
  );
}

export function SeedHeader({ crate, library }: { crate: Crate; library: CrateSummary | undefined }) {
  const s = crate.seed;
  return (
    <header className="flex shrink-0 gap-4 border-b border-line bg-surface px-5 py-4">
      <CoverArt src={s.cover_url} size={92} rounded="md" camelot={s.camelot} alt={s.display_name} className="shadow-card" />
      <div className="flex min-w-0 flex-1 flex-col justify-between gap-2">
        <div className="min-w-0">
          <div className="label-caps flex items-center gap-1.5 !text-accent-hover">
            <Sprout size={11} /> Seed
            <span className="font-medium tracking-normal text-fg-subtle normal-case">
              · {library ? `crate exists — ${plural(library.track_count, "track")}` : "new crate"}
            </span>
          </div>
          <h1 className="mt-0.5 truncate text-2xl leading-tight font-semibold tracking-tight" title={s.name}>
            {s.name}
          </h1>
          <div className="truncate text-md text-fg-muted">
            {s.artists.join(", ")}
            <span className="text-fg-subtle">
              {" "}
              · {s.album} · {fmtReleaseDate(s.release_date)}
            </span>
          </div>
        </div>
        <div className="flex items-end gap-6">
          <Stat label="Key">
            <KeyBadge camelot={s.camelot} keyName={s.key} size="md" />
            <span className="text-sm font-normal text-fg-muted">{s.key}</span>
          </Stat>
          <Stat label="BPM">{fmtBpm(s.bpm)}</Stat>
          <Stat label="Energy">
            {pct01(s.energy)} <Meter value={s.energy} />
          </Stat>
          <Stat label="Dance">
            {pct01(s.danceability)} <Meter value={s.danceability} />
          </Stat>
          <Stat label="Length">{fmtTime(s.duration_s)}</Stat>
          <Stat label="Popularity" className="hidden xl:flex">
            {s.popularity}
          </Stat>
        </div>
      </div>
      <div className="flex w-[300px] shrink-0 flex-col items-end justify-between gap-2">
        <div className="flex items-center gap-0.5">
          <ExtLink href={s.spotify_url}>Spotify</ExtLink>
          <ExtLink href={tunebatUrl(s)}>tunebat</ExtLink>
          {library && (
            <>
              <Tooltip content="Reveal the crate folder in Finder">
                <Button variant="ghost" size="sm" icon={FolderOpen} onClick={() => void api.reveal(library.path)}>
                  Reveal
                </Button>
              </Tooltip>
              <Button size="sm" icon={Layers} onClick={() => navigate({ name: "set", folder: library.folder })}>
                Open set
              </Button>
            </>
          )}
        </div>
        <div className="flex w-full items-center gap-2.5 rounded-md border border-line bg-panel/60 py-1.5 pr-2.5 pl-1.5">
          <PreviewButton track={s} size="md" />
          <PreviewWave track={s} bars={64} height={26} className="min-w-0 flex-1" />
        </div>
      </div>
    </header>
  );
}
