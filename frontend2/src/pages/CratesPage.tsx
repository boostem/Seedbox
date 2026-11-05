import { ArrowDown, ArrowUp, Boxes, Library, ListMusic, Save, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { ArcTemplate, CrateLink, LibraryBridge, LibraryBridges, LongSetResult, SetSourceRef } from "@/api/types";
import { Badge, type BadgeTone, Button, Checkbox, EmptyState, Field, IconButton, Input, Page, PageBody, PageHeader, Panel, PanelHeader, Select, Skeleton, Switch } from "@/components/ui";
import { LibraryRow } from "@/features/sets/BridgePanel";
import { OrderList } from "@/features/sets/OrderList";
import { navigate } from "@/lib/router";
import { toast } from "@/store/toasts";

interface Option extends SetSourceRef {
  name: string;
  /** Null for the whole library, which only says how much it has when built. */
  tracks: number | null;
}

const idOf = (s: SetSourceRef) => `${s.kind}:${s.id ?? ""}`;
const FIT: Record<CrateLink["fit"], { label: string; tone: BadgeTone }> = {
  good: { label: "Mixes well", tone: "ok" },
  fair: { label: "Workable", tone: "neutral" },
  poor: { label: "Clashes", tone: "warn" },
  unknown: { label: "Not analysed", tone: "neutral" },
};
const OTHER_LINKS = 3;

/** The links to show: each crate into the next as they're ordered, then the best others, which might suggest a new order. */
export function linksToShow(links: CrateLink[], chosen: SetSourceRef[]): CrateLink[] {
  const named = chosen.flatMap((s, i) => (s.kind === "library" ? [] : [i]));
  const isNext = (l: CrateLink) => named[named.indexOf(l.from_source) + 1] === l.to_source;
  const next = links.filter(isNext).sort((a, b) => a.from_source - b.from_source);
  return [...next, ...links.filter((l) => !isNext(l) && l.fit === "good").slice(0, OTHER_LINKS)];
}

function LinkRow({ link, names }: { link: CrateLink; names: string[] }) {
  const fit = FIT[link.fit];
  return (
    <li className="flex items-center gap-3 border-b border-line/60 px-4 py-2 last:border-b-0">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm text-fg">
          {names[link.from_source]} → {names[link.to_source]}
        </div>
        <div className="truncate text-xs text-fg-subtle">
          {link.from.title} into {link.to.title}: {link.text}
        </div>
      </div>
      <Badge tone={fit.tone} className="shrink-0">
        {fit.label}
      </Badge>
    </li>
  );
}

function Bridges({ found, adding, onAdd }: { found: LibraryBridges | "loading"; adding: string | null; onAdd: (t: LibraryBridge) => void }) {
  if (found === "loading") return <Skeleton className="mx-4 my-2 h-8" />;
  if (!found.results.length) return <p className="px-4 py-2 text-xs text-fg-muted">Nothing in the library goes smoothly between these two. Save the set to look on tunebat from its page.</p>;
  return (
    <ul className="px-4 py-1">
      {found.results.map((t) => (
        <LibraryRow key={t.hash} track={t} adding={adding === t.hash} onAdd={() => onAdd(t)} />
      ))}
    </ul>
  );
}

/** A set built across crates: choose them in the order they should play, see how they link, build, fill the gaps, save. */
export function CratesPage() {
  const [options, setOptions] = useState<Option[] | null>(null);
  const [arcs, setArcs] = useState<ArcTemplate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Option[]>([]);
  const [keepOrder, setKeepOrder] = useState(true);
  const [arc, setArc] = useState("peak_time");
  const [length, setLength] = useState("");
  const [links, setLinks] = useState<CrateLink[]>([]);
  const [built, setBuilt] = useState<LongSetResult | null>(null);
  const [sourceOf, setSourceOf] = useState<ReadonlyMap<string, string>>(new Map());
  const [busy, setBusy] = useState<"build" | "save" | null>(null);
  const [gap, setGap] = useState<{ at: number; found: LibraryBridges | "loading" } | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [name, setName] = useState("");

  useEffect(() => {
    const ctrl = new AbortController();
    const quiet = { quiet: true, signal: ctrl.signal };
    Promise.all([api.library.crates(quiet), api.sets.list(quiet), api.sets.arcs(quiet)]).then(
      ([crates, sets, templates]) => {
        setOptions([
          ...crates.map((c): Option => ({ kind: "crate", id: c.id, name: c.name, tracks: c.track_count })),
          ...sets.map((s): Option => ({ kind: "set", id: s.id, name: s.name, tracks: s.track_count })),
          { kind: "library", id: null, name: "Whole library", tracks: null },
        ]);
        setArcs(templates);
      },
      (e) => {
        if (!ctrl.signal.aborted) setError((e as Error).message);
      },
    );
    return () => ctrl.abort();
  }, []);

  const sources = useMemo(() => chosen.map(({ kind, id }): SetSourceRef => ({ kind, id })), [chosen]);

  useEffect(() => {
    setLinks([]);
    if (sources.filter((s) => s.kind !== "library").length < 2) return;
    const ctrl = new AbortController();
    api.sets.bridges(sources, { quiet: true, signal: ctrl.signal }).then(
      (r) => setLinks(r.links),
      () => {}, // links are a help: the set still builds without them
    );
    return () => ctrl.abort();
  }, [sources]);

  const toggle = (o: Option) => setChosen((c) => (c.some((x) => idOf(x) === idOf(o)) ? c.filter((x) => idOf(x) !== idOf(o)) : [...c, o]));
  const move = (at: number, by: number) =>
    setChosen((c) => {
      const out = [...c];
      [out[at], out[at + by]] = [out[at + by], out[at]];
      return out;
    });

  const limit = Number(length);
  const badLength = length.trim() !== "" && !(Number.isInteger(limit) && limit >= 2 && limit <= 200);
  const maxTracks = length.trim() === "" || badLength ? null : limit;

  const build = async () => {
    setBusy("build");
    setGap(null);
    try {
      const result = await api.sets.build({ sources, max_tracks: maxTracks, keep_order: keepOrder, arc });
      setBuilt(result);
      const names = result.sources?.map((s) => s.name) ?? [];
      setSourceOf(new Map(result.order.map((t) => [t.hash, t.source === undefined ? "" : (names[t.source] ?? "")])));
      if (!name) setName(names.filter((n) => n !== "Whole library").join(" + ") || "Long set");
    } catch {
      // the client has shown it
    } finally {
      setBusy(null);
    }
  };

  const findBridge = async (at: number) => {
    if (!built) return;
    if (gap?.at === at) {
      setGap(null);
      return;
    }
    setGap({ at, found: "loading" });
    try {
      const found = await api.sets.buildBridge(built.order.map((t) => t.hash), at, { arc });
      setGap((g) => (g?.at === at ? { at, found } : g));
    } catch {
      setGap(null); // the client has shown it
    }
  };

  const addBridge = async (t: LibraryBridge) => {
    if (!built || !gap) return;
    const hashes = built.order.map((x) => x.hash);
    hashes.splice(gap.at + 1, 0, t.hash);
    setAdding(t.hash);
    try {
      const result = await api.sets.explainBuild(hashes, { arc });
      setBuilt({ ...result, sources: built.sources, left_out: built.left_out, moved: built.moved });
      setGap(null);
    } catch {
      // the client has shown it
    } finally {
      setAdding(null);
    }
  };

  const save = async () => {
    if (!built || !name.trim()) return;
    const hashes = built.order.map((t) => t.hash);
    setBusy("save");
    try {
      const made = await api.sets.create(name.trim(), hashes);
      await api.sets.applyOrder(made.id, hashes, { arc, tempo_start: null, tempo_end: null, pins: [] });
      toast.success(`Saved ${name.trim()}`);
      navigate({ name: "sets" });
    } catch {
      // the client has shown it
    } finally {
      setBusy(null);
    }
  };

  if (error) {
    return (
      <Page>
        <PageHeader title="Crates" />
        <PageBody>
          <EmptyState icon={Boxes} title="Couldn't load your crates" description={error} />
        </PageBody>
      </Page>
    );
  }

  const shown = linksToShow(links, sources);
  const names = chosen.map((c) => c.name);
  const gaps = built?.transitions.filter((t) => t.gap).length ?? 0;
  const facts = built
    ? [
        `${built.order.length} tracks`,
        built.left_out ? `${built.left_out} left out for the length` : null,
        built.moved ? `${built.moved} moved out of their crate to mix better` : null,
        gaps ? `${gaps} ${gaps === 1 ? "gap" : "gaps"} to look at` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : undefined;

  return (
    <Page>
      <PageHeader title="Crates" subtitle="Build one long set across crates, sets or the whole library" />
      <PageBody>
        {!options ? (
          <div className="flex max-w-4xl flex-col gap-3">
            <Skeleton className="h-48" />
            <Skeleton className="h-32" />
          </div>
        ) : (
          <div className="flex max-w-4xl flex-col gap-3">
            <Panel>
              <PanelHeader title="What goes in" description="Tick them in the order they should play." />
              <ul className="flex flex-col">
                {options.map((o) => {
                  const at = chosen.findIndex((c) => idOf(c) === idOf(o));
                  const Icon = o.kind === "library" ? Library : o.kind === "set" ? ListMusic : Boxes;
                  return (
                    <li key={idOf(o)} className="flex h-control-xl items-center gap-3 border-b border-line/60 px-4 last:border-b-0">
                      <Checkbox checked={at >= 0} onChange={() => toggle(o)} label={o.name} />
                      <Icon size={14} className="shrink-0 text-fg-subtle" aria-hidden />
                      <span className="num text-xs text-fg-subtle">{o.tracks === null ? "" : `${o.tracks} tracks`}</span>
                      <div className="flex-1" />
                      {at >= 0 && (
                        <>
                          <span className="num text-xs text-fg-muted">plays {at + 1}.</span>
                          <IconButton size="sm" icon={ArrowUp} label={`Play ${o.name} earlier`} disabled={at === 0} onClick={() => move(at, -1)} />
                          <IconButton size="sm" icon={ArrowDown} label={`Play ${o.name} later`} disabled={at === chosen.length - 1} onClick={() => move(at, 1)} />
                        </>
                      )}
                    </li>
                  );
                })}
              </ul>
              <div className="flex flex-wrap items-end gap-3 border-t border-line p-4">
                <Field label="Energy arc" className="w-40">
                  <Select aria-label="Energy arc" value={arc} onChange={(e) => setArc(e.target.value)}>
                    {arcs.map((a) => (
                      <option key={a.name} value={a.name}>
                        {a.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Longest, 2 to 200" className="w-64">
                  <Input inputMode="numeric" suffix="tracks" aria-label="Longest" aria-invalid={badLength} placeholder="All, or 60 with the library" value={length} onChange={(e) => setLength(e.target.value)} />
                </Field>
                <Switch checked={keepOrder} onChange={setKeepOrder} label="Keep the crates in order" />
                <div className="flex-1" />
                <Button variant="primary" icon={Sparkles} onClick={build} loading={busy === "build"} disabled={!chosen.length || badLength}>
                  Build the set
                </Button>
              </div>
            </Panel>

            {shown.length > 0 && (
              <Panel>
                <PanelHeader title="Links between them" description="The best pair from the end of one to the start of the next." />
                <ul>
                  {shown.map((l) => (
                    <LinkRow key={`${l.from_source}-${l.to_source}`} link={l} names={names} />
                  ))}
                </ul>
              </Panel>
            )}

            {built && (
              <Panel>
                <PanelHeader title="Proposed set" description={facts} />
                {built.order.length ? (
                  <>
                    <OrderList
                      result={built}
                      onBridge={findBridge}
                      bridgeAt={gap?.at ?? null}
                      bridgePanel={gap && <Bridges found={gap.found} adding={adding} onAdd={addBridge} />}
                      label={(t) => sourceOf.get(t.hash) || undefined}
                    />
                    <div className="flex items-center justify-end gap-2 border-t border-line p-3">
                      <Input aria-label="Name of the set" value={name} onChange={(e) => setName(e.target.value)} className="w-56" />
                      <Button variant="primary" icon={Save} onClick={save} loading={busy === "save"} disabled={!name.trim()}>
                        Save as a set
                      </Button>
                    </div>
                  </>
                ) : (
                  <EmptyState icon={ListMusic} title="Nothing to build from" description="These have no tracks yet." />
                )}
              </Panel>
            )}
          </div>
        )}
      </PageBody>
    </Page>
  );
}
