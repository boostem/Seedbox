import { Check, Pause, Play } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { ListenScore, ListenScores, ListenSlot, ListenTrial } from "@/api/types";
import { player, usePlayer } from "@/audio/player";
import { Button, EmptyState, Field, Kbd, Panel, PanelHeader, Spinner } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useHotkey } from "@/lib/hotkeys";
import { toast } from "@/store/toasts";
import { loadRater, SCORES, slotName, SLOTS, withScore } from "./rating";

const prefix = (trialId: string) => `listen:${trialId}:`;

function ListenButton({ trialId, slot, heard, onListen }: { trialId: string; slot: ListenSlot; heard: boolean; onListen: (slot: ListenSlot) => void }) {
  const id = prefix(trialId) + slot;
  const status = usePlayer((s) => (s.currentId === id ? s.status : null));
  const playing = status === "playing" || status === "loading";
  return (
    <button
      type="button"
      aria-pressed={playing}
      onClick={() => onListen(slot)}
      className={cn(
        "flex flex-1 items-center gap-3 rounded-md border bg-raised p-4 text-left transition-colors hover:border-fg-faint",
        playing ? "border-accent" : "border-line-strong",
      )}
    >
      {playing ? <Pause size={20} className="shrink-0 text-accent" /> : <Play size={20} className="shrink-0 text-fg-subtle" />}
      <span className="min-w-0 flex-1">
        <span className="block text-md font-medium text-fg">Version {slotName(slot)}</span>
        <span className="block text-xs text-fg-subtle">{status === "error" ? "This render won't play" : playing ? "Playing" : heard ? "Heard" : "Not heard yet"}</span>
      </span>
      {heard && <Check size={14} className="shrink-0 text-fg-subtle" />}
      <Kbd>{slotName(slot)}</Kbd>
    </button>
  );
}

function ScoreButtons({ label, value, onChange }: { label: string; value: number | undefined; onChange: (v: number) => void }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex gap-1">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          role="radio"
          aria-checked={value === n}
          onClick={() => onChange(n)}
          className={cn(
            "num size-control-md rounded-sm border text-sm transition-colors",
            value === n ? "border-accent bg-accent/20 text-fg" : "border-line-strong text-fg-subtle hover:border-fg-faint hover:text-fg",
          )}
        >
          {n}
        </button>
      ))}
    </div>
  );
}

/** One trial: listen to both, say which is better. Everything else is optional. Nothing here knows what A and B are. */
function Trial({ trial, rater, onDone }: { trial: ListenTrial; rater: string; onDone: () => void }) {
  const [heard, setHeard] = useState<Record<ListenSlot, boolean>>({ a: false, b: false });
  const [choice, setChoice] = useState<ListenSlot | null>(null);
  const [scores, setScores] = useState<ListenScores>({});
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  // only this trial's audio: never touch what the listener had playing unless they press play here
  useEffect(() => () => {
    if (usePlayer.getState().currentId?.startsWith(prefix(trial.id))) player.stop();
  }, [trial.id]);

  /** Switching keeps the position, so both versions can be compared at the same moment. */
  const listen = useCallback((slot: ListenSlot) => {
    const id = prefix(trial.id) + slot;
    const s = usePlayer.getState();
    setHeard((h) => (h[slot] ? h : { ...h, [slot]: true }));
    if (s.currentId === id && s.status !== "error") return player.playPause();
    const startAt = s.currentId?.startsWith(prefix(trial.id)) ? player.time() : 0;
    void player.play(id, api.listen.audioUrl(trial.id, slot), { startAt, title: "Blind test", subtitle: `Version ${slotName(slot)}` });
  }, [trial.id]);

  useHotkey("a", () => listen("a"));
  useHotkey("b", () => listen("b"));

  const submit = async () => {
    if (!choice) return;
    setBusy(true);
    try {
      await api.listen.rate(trial.id, { choice, scores, comment, rater }, { quiet: true });
      onDone();
    } catch (e) {
      setBusy(false);
      if (e instanceof ApiError && e.isNotFound) {
        toast.warning("That trial had ended, so here is a new one.");
        onDone();
      } else {
        toast.error(`Couldn't save the rating: ${(e as Error).message}`);
      }
    }
  };

  const both = heard.a && heard.b;
  return (
    <Panel>
      <PanelHeader title={trial.pair} description={`Comparison ${Math.min(trial.progress.done + 1, trial.progress.total)} of ${trial.progress.total}. Two versions of this transition, in random order.`} />
      <div className="flex flex-col gap-4 p-4">
        <div className="flex gap-3">
          {SLOTS.map((slot) => <ListenButton key={slot} trialId={trial.id} slot={slot} heard={heard[slot]} onListen={listen} />)}
        </div>

        <div>
          <div className="label-caps mb-2">Which is better?</div>
          <div className="flex items-center gap-2">
            {SLOTS.map((slot) => (
              <Button key={slot} variant={choice === slot ? "primary" : "secondary"} size="md" disabled={!both} aria-pressed={choice === slot} onClick={() => setChoice(slot)}>
                {slotName(slot)} is better
              </Button>
            ))}
            {!both && <span className="text-xs text-fg-subtle">Listen to both first.</span>}
          </div>
        </div>

        <table className="w-fit border-separate border-spacing-y-1 text-sm">
          <thead>
            <tr>
              <th className="label-caps pr-6 text-left font-semibold">Optional, 1–5</th>
              {SLOTS.map((slot) => <th key={slot} className="label-caps pr-6 text-left font-semibold">Version {slotName(slot)}</th>)}
            </tr>
          </thead>
          <tbody>
            {SCORES.map(({ id, label }) => (
              <tr key={id}>
                <td className="pr-6 text-fg-muted">{label}</td>
                {SLOTS.map((slot) => (
                  <td key={slot} className="pr-6">
                    <ScoreButtons label={`${label}, version ${slotName(slot)}`} value={scores[slot]?.[id as ListenScore]} onChange={(v) => setScores((s) => withScore(s, slot, id, v))} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>

        <Field label="Notes (optional)">
          <textarea
            value={comment}
            maxLength={2000}
            rows={2}
            onChange={(e) => setComment(e.target.value)}
            placeholder="What did you hear?"
            className="w-full resize-y rounded-sm border border-line-strong bg-raised px-2 py-1.5 text-sm text-fg outline-none placeholder:text-fg-faint hover:border-fg-faint focus:border-accent"
          />
        </Field>

        <div>
          <Button variant="primary" size="md" disabled={!choice} loading={busy} onClick={() => void submit()}>Save and next</Button>
        </div>
      </div>
    </Panel>
  );
}

/** Deals trials until the page closes. Each trial gets a fresh form through its key. */
export function TrialPanel({ rater, folder, onRated }: { rater: string; folder: string | undefined; onRated: () => void }) {
  const [trial, setTrial] = useState<ListenTrial | null | undefined>(undefined);
  const deal = useCallback(async () => {
    setTrial(undefined);
    try {
      // the saved name, not a prop: typing a name shouldn't throw away the trial being listened to
      setTrial(await api.listen.next(loadRater(), { quiet: true }));
    } catch (e) {
      if (!(e instanceof ApiError && e.isNotFound)) toast.error(`Couldn't start a trial: ${(e as Error).message}`);
      setTrial(null);
    }
  }, []);
  useEffect(() => void deal(), [deal]);

  if (trial === undefined) return <div className="flex justify-center p-8"><Spinner /></div>;
  if (trial === null) {
    return (
      <EmptyState
        title="Nothing to compare yet"
        description={
          <>
            A comparison needs two renders of one pair, as {folder ? <code className="font-mono">{folder}/&lt;pair&gt;/&lt;version&gt;[--&lt;style&gt;].wav</code> : "files in the listening folder"}.
            Make some with <code className="font-mono">python -m seedbox2.eval listen</code>, or drop in a DJ's version as <code className="font-mono">human--Their name.mp3</code>.
          </>
        }
      />
    );
  }
  return <Trial key={trial.id} trial={trial} rater={rater} onDone={() => {
    onRated();
    void deal();
  }} />;
}
