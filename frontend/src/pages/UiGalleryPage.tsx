/** #/ui — living reference of the design system (not linked in the sidebar). */
import { Download, FolderOpen, Library, Play, RefreshCw, Search, Sparkles, Square } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { api, MOCK } from "@/api/client";
import type { Move } from "@/api/types";
import { player, useIsPlaying } from "@/audio/player";
import { usePageTitle } from "@/components/shell/pageTitle";
import {
  Badge,
  Button,
  CamelotWheel,
  Checkbox,
  CoverArt,
  EmptyState,
  Field,
  IconButton,
  Input,
  Kbd,
  KeyBadge,
  Modal,
  MoveChip,
  Page,
  PageBody,
  PageHeader,
  Panel,
  PanelHeader,
  PeaksWaveform,
  ProgressBar,
  ScoreChip,
  Slider,
  Spinner,
  StarRating,
  StatusDot,
  Table,
  TableScroll,
  Tabs,
  TBody,
  Td,
  Th,
  THead,
  Tooltip,
  Tr,
} from "@/components/ui";
import { CAMELOT_CODES } from "@/lib/camelot";
import { fmtBpm, fmtTime } from "@/lib/format";
import { useHashQuery } from "@/lib/router";
import { useApp } from "@/store/app";
import { toast } from "@/store/toasts";

const MOVES: Move[] = ["same", "+1", "-1", "relative", "+2 boost", "+7 boost", "diagonal", "clash", "?"];
const DEMO_PEAKS = Array.from({ length: 200 }, (_, i) => Math.min(1, 0.35 + 0.25 * Math.abs(Math.sin(i / 9)) + (i % 6 === 0 ? 0.3 : 0) + ((i * 7919) % 13) / 60));
const DEMO = [
  { id: "a", name: "Favour", artists: "FISHER, Tones And I", camelot: "1A", key: "G♯ minor", bpm: 130, dur: 194.9, score: 100, move: "same" as Move, passed: true, stars: 4 },
  { id: "b", name: "Don't Stop", artists: "Prospa", camelot: "1B", key: "B major", bpm: 128, dur: 147.7, score: 84, move: "relative" as Move, passed: true, stars: 4 },
  { id: "c", name: "Midnight Trip", artists: "TOYZZ", camelot: "12A", key: "C♯ minor", bpm: 132, dur: 194.5, score: 79, move: "-1" as Move, passed: true, stars: 5 },
  { id: "d", name: "Rain", artists: "FISHER", camelot: "7A", key: "D minor", bpm: 130, dur: 208.6, score: 27, move: "clash" as Move, passed: false, stars: 3 },
];

function Section({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <Panel className={className}>
      <PanelHeader title={title} />
      <div className="flex flex-wrap items-center gap-3 px-4 py-4">{children}</div>
    </Panel>
  );
}

function PlayDemo() {
  const favour = useApp((s) => s.crates?.find((c) => c.seed)?.seed);
  const playing = useIsPlaying(favour?.id);
  return (
    <Button
      icon={playing ? Square : Play}
      disabled={!favour}
      onClick={() => favour && (playing ? player.stop() : player.playPreview(favour))}
    >
      {playing ? "Stop" : "player.playPreview(seed)"}
    </Button>
  );
}

/** Mock only: #/ui?demo=activity opens the Favour crate (preview preloading), starts a download job and plays a preview. */
function useMockDemo(demo: string | null) {
  useEffect(() => {
    if (!MOCK || demo !== "activity") return;
    let cancelled = false;
    void (async () => {
      const crate = await api.newCrate("44wR4NqR75eJMETxrz59Sr");
      if (cancelled) return;
      const ticked = crate.recs.filter((r) => r.ticked_default && !r.existing_path).map((r) => r.track.id);
      const job = await api.startJob(crate.seed.id, ticked.slice(0, 6));
      useApp.getState().upsertJob(job);
      useApp.getState().select({ kind: "preview", track: crate.seed });
      void player.playPreview(crate.seed);
    })();
    return () => {
      cancelled = true;
    };
  }, [demo]);
}

export function UiGalleryPage() {
  usePageTitle("Design system");
  useMockDemo(useHashQuery().get("demo"));
  const [tab, setTab] = useState<"a" | "b" | "c">("a");
  const [seg, setSeg] = useState<"out" | "in">("out");
  const [checked, setChecked] = useState(true);
  const [vol, setVol] = useState(0.6);
  const [modal, setModal] = useState(false);
  const [sel, setSel] = useState("b");
  const [active, setActive] = useState<string | null>("1A");

  return (
    <Page>
      <PageHeader title="Design system" subtitle="Seedbox UI kit — import from @/components/ui (see frontend/DESIGN.md)" />
      <PageBody className="px-5 py-5">
        <div className="mx-auto grid max-w-[1240px] grid-cols-2 gap-5">
          <Section title="Buttons">
            <Button variant="primary" icon={Download} kbd="Mod+D">
              Download 12
            </Button>
            <Button icon={RefreshCw}>Secondary</Button>
            <Button variant="outline">Outline</Button>
            <Button variant="ghost" icon={FolderOpen}>
              Ghost
            </Button>
            <Button variant="danger">Cancel job</Button>
            <Button loading>Loading</Button>
            <Button size="xs">xs</Button>
            <Button size="md" variant="primary">
              md
            </Button>
            <IconButton icon={Search} label="Icon button" />
            <IconButton icon={Library} label="Active" active />
            <IconButton icon={Sparkles} label="Secondary" variant="secondary" />
          </Section>

          <Section title="Badges · status">
            <Badge>neutral</Badge>
            <Badge tone="accent">accent</Badge>
            <Badge tone="ok">downloaded</Badge>
            <Badge tone="info">copied</Badge>
            <Badge tone="neutral" variant="outline">
              exists
            </Badge>
            <Badge tone="warn">skipped</Badge>
            <Badge tone="danger">failed</Badge>
            <Badge tone="accent" variant="solid">
              SEED
            </Badge>
            <StatusDot tone="ok" label="Live" />
            <StatusDot tone="warn" pulse label="Reconnecting" />
            <Spinner />
            <Kbd combo="Space" />
            <Kbd combo="Mod+Shift+K" />
          </Section>

          <Section title="Camelot keys (lib/camelot.ts — single colour source)" className="col-span-2">
            <div className="grid w-full grid-cols-12 gap-1.5">
              {CAMELOT_CODES.filter((c) => c.endsWith("A")).map((c) => (
                <KeyBadge key={c} camelot={c} size="md" className="w-full" />
              ))}
              {CAMELOT_CODES.filter((c) => c.endsWith("B")).map((c) => (
                <KeyBadge key={c} camelot={c} size="md" className="w-full" />
              ))}
              {CAMELOT_CODES.filter((c) => c.endsWith("A")).map((c) => (
                <KeyBadge key={c} camelot={c} size="md" variant="solid" className="w-full" />
              ))}
            </div>
            <div className="flex items-center gap-2">
              <KeyBadge camelot="8A" size="xs" /> <KeyBadge camelot="8A" /> <KeyBadge camelot="8A" variant="text" /> <KeyBadge camelot="?" /> <KeyBadge camelot="5B" move="clash" />
            </div>
          </Section>

          <Section title="Moves · scores">
            {MOVES.map((m) => (
              <MoveChip key={m} move={m} />
            ))}
            <div className="flex w-full items-center gap-2">
              {[100, 86, 72, 61, 48].map((v, i) => (
                <ScoreChip key={v} value={v} move={MOVES[i]} />
              ))}
              <ScoreChip value={41} move="+2 boost" passed={false} />
              <ScoreChip value={27} move="clash" passed={false} />
              <ScoreChip value={88} move="same" size="md" />
            </div>
          </Section>

          <Section title="Camelot wheel">
            <CamelotWheel size={200} counts={{ "1A": 5, "1B": 3, "12A": 2, "2A": 2, "7A": 1 }} active={active} onSelect={setActive} />
            <CamelotWheel size={200} path={["1A", "1B", "12A", "12B", "1B", "2B", "2A"]} center="set" />
          </Section>

          <Section title="Inputs">
            <Field label="Search" hint="Free text, Spotify URL or track ID" className="w-full">
              <Input icon={Search} size="lg" placeholder="Artist, title…" />
            </Field>
            <Input placeholder="Small input" suffix="s" wrapperClassName="w-40" />
            <Input placeholder="Invalid" invalid wrapperClassName="w-40" />
            <Checkbox checked={checked} onChange={setChecked} label="Tick passing tracks" />
            <Checkbox checked={false} indeterminate onChange={() => undefined} label="Indeterminate" />
            <Checkbox checked onChange={() => undefined} color="var(--color-move-relative)" />
            <div className="flex w-56 items-center gap-2">
              <Slider value={vol} onChange={setVol} label="Demo" />
              <span className="w-8 text-xs text-fg-muted num">{Math.round(vol * 100)}</span>
            </div>
          </Section>

          <Section title="Tabs · progress · stars">
            <Tabs
              className="w-full"
              value={tab}
              onChange={setTab}
              items={[
                { id: "a", label: "Recommendations", count: 19 },
                { id: "b", label: "Ticked", count: 13 },
                { id: "c", label: "Already in crate", count: 4 },
              ]}
            />
            <Tabs variant="segmented" value={seg} onChange={setSeg} items={[{ id: "out", label: "Mix out" }, { id: "in", label: "Mix in" }]} />
            <div className="flex w-full flex-col gap-2">
              <ProgressBar value={0.62} />
              <ProgressBar value={0.3} tone="ok" size="md" />
              <ProgressBar value={null} tone="info" size="xs" />
            </div>
            <StarRating rating={7} />
            <StarRating stars={2} size={14} />
          </Section>

          <Section title="Preview waveform · player">
            <div className="w-full">
              <PeaksWaveform peaks={DEMO_PEAKS} progress={0.38} height={36} />
            </div>
            <div className="w-full">
              <PeaksWaveform peaks={null} loading height={20} />
            </div>
            <PlayDemo />
            <Button onClick={() => toast.success("Crate ready: 11 downloaded · 2 exists", { title: "FISHER - Favour" })}>Toast success</Button>
            <Button onClick={() => toast.error("HTTP Error 403: Forbidden", { title: "Download failed", action: { label: "Retry", onClick: () => undefined } })}>Toast error</Button>
            <Button onClick={() => setModal(true)}>Open modal</Button>
            <Tooltip content="Tooltips render in a portal">
              <Badge tone="info">hover me</Badge>
            </Tooltip>
          </Section>

          <Panel className="col-span-2 overflow-hidden">
            <PanelHeader title="Table" description="Dense rows, sticky header, hover/selection, dimmed rule-failing row" />
            <TableScroll className="max-h-64">
              <Table>
                <THead>
                  <Tr interactive={false}>
                    <Th width={36} />
                    <Th width={44} />
                    <Th width={48} align="center" sort="desc" onSort={() => undefined}>
                      Score
                    </Th>
                    <Th>Title</Th>
                    <Th width={56}>Key</Th>
                    <Th width={56} align="right">
                      BPM
                    </Th>
                    <Th width={56} align="right">
                      Time
                    </Th>
                    <Th width={88}>Energy</Th>
                  </Tr>
                </THead>
                <TBody>
                  {DEMO.map((r) => (
                    <Tr key={r.id} selected={sel === r.id} dim={!r.passed} onClick={() => setSel(r.id)}>
                      <Td className="pl-3">
                        <Checkbox checked={r.passed} onChange={() => undefined} />
                      </Td>
                      <Td className="pr-0">
                        <CoverArt src={null} size={24} camelot={r.camelot} />
                      </Td>
                      <Td align="center">
                        <ScoreChip value={r.score} move={r.move} passed={r.passed} />
                      </Td>
                      <Td truncate>
                        <span className={r.passed ? "font-medium text-fg" : ""}>{r.name}</span> <span className="text-fg-muted">· {r.artists}</span>
                      </Td>
                      <Td>
                        <KeyBadge camelot={r.camelot} keyName={r.key} dim={!r.passed} />
                      </Td>
                      <Td align="right" num>
                        {fmtBpm(r.bpm)}
                      </Td>
                      <Td align="right" num muted>
                        {fmtTime(r.dur)}
                      </Td>
                      <Td>
                        <StarRating stars={r.stars} />
                      </Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            </TableScroll>
          </Panel>

          <Panel className="col-span-2">
            <EmptyState icon={Library} title="Empty state" description="Use for empty lists, errors and 'nothing selected' panels." action={<Button variant="primary">Primary action</Button>} />
          </Panel>
        </div>
        <Modal
          open={modal}
          onClose={() => setModal(false)}
          title="Download 12 tracks?"
          description="They'll be saved to ~/Music/MusicReco/FISHER, Tones And I - Favour"
          footer={
            <>
              <Button variant="ghost" onClick={() => setModal(false)}>
                Cancel
              </Button>
              <Button variant="primary" icon={Download} onClick={() => setModal(false)}>
                Download
              </Button>
            </>
          }
        >
          <p className="text-sm text-fg-muted">Modal body. Esc or a backdrop click closes it; global shortcuts pause while it's open.</p>
        </Modal>
      </PageBody>
    </Page>
  );
}
