import { AudioLines, ListMusic } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { SetSession, SetSummary } from "@/api/types";
import { Button, EmptyState, Skeleton } from "@/components/ui";
import { paintStats, NowPlaying } from "@/features/nowplaying/NowPlaying";
import { type NowPlayingDriver, SetDriver } from "@/features/nowplaying/driver";
import { playableRun } from "@/features/nowplaying/session";
import { setFrameCap } from "@/lib/frameCap";
import { demoNowPlaying } from "@/lib/demo/nowPlaying";
import { href, Link, navigate, useHashQuery } from "@/lib/router";

/** `window.__seedboxNowPlaying`: what `python -m seedbox2.bench frontend` drives on the demo, and tests on a real set. */
declare global {
  interface Window {
    __seedboxNowPlaying?: { driver: NowPlayingDriver; run?: (on: boolean) => void; frameCap: (fps: number | null) => void; draws: () => number };
  }
}

function Demo() {
  const driver = useMemo(demoNowPlaying, []);
  useEffect(() => {
    window.__seedboxNowPlaying = { driver, run: driver.setRunning, frameCap: setFrameCap, draws: () => paintStats.draws };
    return () => {
      driver.close();
      delete window.__seedboxNowPlaying;
    };
  }, [driver]);
  return <NowPlaying driver={driver} />;
}

function LiveSet({ session }: { session: SetSession }) {
  const [driver, setDriver] = useState<NowPlayingDriver | null>(null);
  useEffect(() => {
    const d = new SetDriver(session);
    setDriver(d);
    window.__seedboxNowPlaying = { driver: d, frameCap: setFrameCap, draws: () => paintStats.draws };
    return () => {
      d.close();
      delete window.__seedboxNowPlaying;
    };
  }, [session]);
  return driver ? <NowPlaying driver={driver} /> : null;
}

function Waiting({ title, description, action }: { title: string; description: string; action?: React.ReactNode }) {
  return <EmptyState className="h-full" icon={AudioLines} title={title} description={description} action={action ?? <Button onClick={() => navigate({ name: "sets" })}>Go to Sets</Button>} />;
}

function OneSet({ id }: { id: number }) {
  const [session, setSession] = useState<SetSession | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    api.sets.session(id).then((s) => live && setSession(s), () => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [id]);
  if (failed) return <Waiting title="Couldn't open this set" description="It may have been deleted." />;
  if (!session) return <Skeleton className="m-4 h-full" />;
  if (session.tracks.length < 2) return <Waiting title={`${session.name} has nothing to mix yet`} description="A set needs two tracks and a plan between them." />;
  if (!playableRun(session)) return <Waiting title={`${session.name} has no transition plans yet`} description="Plans come from Seedbox 1's edited mixes for now; planning a set here arrives with #61." />;
  return <LiveSet session={session} />;
}

function Chooser() {
  const [sets, setSets] = useState<SetSummary[] | null>(null);
  useEffect(() => {
    let live = true;
    api.sets.list().then((s) => live && setSets(s), () => live && setSets([]));
    return () => {
      live = false;
    };
  }, []);
  if (!sets) return <Skeleton className="m-4 h-full" />;
  if (!sets.length) return <Waiting title="No sets to play" description="Make a set from the library, then open it here." />;
  return (
    <div className="mx-auto flex h-full max-w-md flex-col justify-center gap-2 p-4">
      <h1 className="text-lg font-semibold text-fg">Which set?</h1>
      {sets.map((s) => (
        <Link key={s.id} to={`${href({ name: "nowPlaying" })}?set=${s.id}`} className="flex items-center gap-2 rounded-md border border-line bg-panel px-3 py-2 text-base text-fg transition-colors hover:bg-raised">
          <ListMusic size={16} className="text-fg-subtle" aria-hidden />
          <span className="min-w-0 flex-1 truncate">{s.name}</span>
          <span className="text-xs text-fg-subtle num">{`${s.track_count} tracks`}</span>
        </Link>
      ))}
    </div>
  );
}

/** #/now-playing: the decks of a set (?set=ID), full screen, or which set to open; ?demo=1 on synthetic data. */
export function NowPlayingPage() {
  const query = useHashQuery();
  useEffect(() => {
    document.title = "Now playing · Seedbox 2";
  }, []);
  const set = Number(query.get("set"));
  const body = query.get("demo") ? <Demo /> : set ? <OneSet key={set} id={set} /> : <Chooser />;
  return <div className="h-full bg-bg">{body}</div>;
}
