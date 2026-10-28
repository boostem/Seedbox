/**
 * The set builder's selection: a track or a transition, opened from the deep link (?t=<n> track, ?x=<n> transition,
 * 1-based), repaired when the set changes and moved with ↑/↓. Also owns the page's hotkeys (⌥↑/⌥↓ move, ⌘Z undo,
 * Space audition, Enter notes, Esc stop) and which transition cards have their notes open.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { player, usePlayer } from "@/audio/player";
import { useHotkey } from "@/lib/hotkeys";
import { useHashQuery } from "@/lib/router";
import { useListNav } from "@/lib/useListNav";
import { useApp } from "@/store/app";
import { audition, useAudition } from "./audition";
import { requestPlay } from "./mix/pending";
import type { SetSelection } from "./SetList";
import type { SetModel } from "./useSetModel";

const selKey = (s: SetSelection) => (!s ? null : s.kind === "track" ? `t:${s.id}` : `x:${s.from}>${s.to}`);

export function useSetSelection(m: SetModel) {
  const set = m.set;
  const [selection, setSelection] = useState<SetSelection>(null);
  const query = useHashQuery();
  const [initial] = useState(() => ({ t: Number(query.get("t")), x: Number(query.get("x")) }));
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const roughKey = useAudition((s) => (s.status !== "idle" ? s.key : null));
  // the rendered transition plays through the global player as "mix:<from>><to>"
  const renderKey = usePlayer((s) => (s.currentId?.startsWith("mix:") && (s.status === "playing" || s.status === "loading") ? s.currentId.slice(4) : null));
  const auditionKey = roughKey ?? renderKey;

  // stop an audition when leaving the page
  useEffect(() => () => audition.stop(), []);

  // default / repair selection when the set changes
  useEffect(() => {
    if (!set?.tracks.length) return;
    setSelection((s) => {
      if (s?.kind === "track" && set.tracks.some((t) => t.track.id === s.id)) return s;
      if (s?.kind === "transition") {
        if (set.transitions.some((t) => t.from_id === s.from && t.to_id === s.to)) return s;
        const t = set.transitions.find((x) => x.from_id === s.from);
        return t ? { kind: "transition", from: t.from_id, to: t.to_id } : { kind: "track", id: s.from };
      }
      const x = set.transitions[initial.x - 1];
      if (x) return { kind: "transition", from: x.from_id, to: x.to_id };
      return { kind: "track", id: (set.tracks[initial.t - 1] ?? set.tracks[0]).track.id };
    });
  }, [set, initial]);

  // Space plays the selected track (global hotkey) — keep the store selection in sync
  const selTrack = selection?.kind === "track" ? set?.tracks.find((t) => t.track.id === selection.id) : undefined;
  useEffect(() => {
    if (selTrack) useApp.getState().select({ kind: "file", track: selTrack.track, url: selTrack.audio_url });
  }, [selTrack]);

  const select = useCallback((s: SetSelection) => setSelection(s), []);
  const navIds = useMemo(() => {
    if (!set) return [];
    const out: string[] = [];
    set.tracks.forEach((t, i) => {
      out.push(`t:${t.track.id}`);
      const x = set.transitions[i];
      if (x && i + 1 < set.tracks.length) out.push(`x:${x.from_id}>${x.to_id}`);
    });
    return out;
  }, [set]);
  useListNav(navIds, selKey(selection), (id) => {
    if (id.startsWith("t:")) setSelection({ kind: "track", id: id.slice(2) });
    else {
      const [from, to] = id.slice(2).split(">");
      setSelection({ kind: "transition", from, to });
    }
  });

  const tIndex = selection?.kind === "transition" && set ? set.transitions.findIndex((t) => t.from_id === selection.from && t.to_id === selection.to) : -1;
  const selTransition = tIndex >= 0 && set ? set.transitions[tIndex] : null;

  // play a transition: open its mix view, which renders + plays it (or falls back to the rough audition)
  const doAudition = useCallback(
    (key: string) => {
      if (!set) return;
      const t = set.transitions.find((x) => `${x.from_id}>${x.to_id}` === key);
      if (!t) return;
      if (audition.isActive(key)) return audition.stop();
      if (usePlayer.getState().currentId === `mix:${key}`) return player.stop();
      setSelection({ kind: "transition", from: t.from_id, to: t.to_id });
      requestPlay(key);
    },
    [set],
  );

  const toggleExpanded = useCallback(
    (k: string) =>
      setExpanded((s) => {
        const n = new Set(s);
        if (n.has(k)) n.delete(k);
        else n.add(k);
        return n;
      }),
    [],
  );

  useHotkey("Alt+ArrowUp", () => selection?.kind === "track" && m.moveBy(selection.id, -1), { enabled: selection?.kind === "track" });
  useHotkey("Alt+ArrowDown", () => selection?.kind === "track" && m.moveBy(selection.id, 1), { enabled: selection?.kind === "track" });
  useHotkey("Mod+z", () => m.undo(), { enabled: m.canUndo });
  useHotkey("Space", () => {
    if (selection?.kind !== "transition" || !selTransition?.ready) return false;
    doAudition(`${selTransition.from_id}>${selTransition.to_id}`);
  });
  useHotkey("Enter", () => {
    if (selection?.kind !== "transition") return false;
    toggleExpanded(`${selection.from}>${selection.to}`);
  });
  useHotkey("Escape", () => (roughKey ? audition.stop() : renderKey ? player.stop() : false), { enabled: !!auditionKey });

  return { selection, select, selTrack, selTransition, tIndex, expanded, toggleExpanded, auditionKey, doAudition };
}
