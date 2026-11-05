/** The shell's commands: going places, the actions the ticket lists, playback and the app itself (lib/commands.ts). */
import { ArrowLeft, ArrowRight, AudioLines, AudioWaveform, Disc3, FileOutput, House, Keyboard, ListPlus, Palette, Pause, Sparkles, Square, Volume1, Volume2 } from "lucide-react";
import { player, usePlayer } from "@/audio/player";
import type { Command } from "@/lib/commands";
import { navigate, type Route } from "@/lib/router";
import { reopenOnboarding } from "@/store/onboarding";
import { toast } from "@/store/toasts";
import { ui } from "@/store/ui";
import { RAIL, SETTINGS_NAV } from "./nav";

const go = (route: Route) => () => navigate(route);

/** An action whose feature lands with a later ticket: it opens the area and says so. */
function soon(area: Route, what: string, ticket: string): () => void {
  return () => {
    navigate(area);
    toast.info(`${what} arrives with ${ticket}.`, { key: "soon" });
  };
}

const hasTrack = () => usePlayer.getState().currentId !== null;

export function appCommands(): Command[] {
  return [
    { id: "go.home", title: "Go to Home", group: "Go to", icon: House, keywords: "start", run: go({ name: "home" }) },
    ...[...RAIL, SETTINGS_NAV].map(
      (e): Command => ({ id: `go.${e.route}`, title: `Go to ${e.label}`, group: "Go to", icon: e.icon, combo: e.combo, run: go({ name: e.route }) }),
    ),
    { id: "go.mixes", title: "Go to Mixes", group: "Go to", icon: Disc3, combo: "Mod+5", keywords: "transitions", run: go({ name: "mixes" }) },
    { id: "go.nowPlaying", title: "Go to Now playing", group: "Go to", icon: AudioLines, keywords: "decks booth set live", run: go({ name: "nowPlaying" }) },
    { id: "go.ui", title: "Go to Design system", group: "Go to", icon: Palette, keywords: "tokens components ui", run: go({ name: "ui" }) },
    { id: "go.back", title: "Back", group: "Go to", icon: ArrowLeft, combo: "Mod+[", keywords: "previous history", run: () => history.back() },
    { id: "go.forward", title: "Forward", group: "Go to", icon: ArrowRight, combo: "Mod+]", keywords: "next history", run: () => history.forward() },

    { id: "library.analyse", title: "Analyse library", group: "Actions", icon: AudioWaveform, combo: "Mod+Shift+A", hint: "Soon", keywords: "beats key waveform stems", run: soon({ name: "library" }, "Analysis", "#43–#49") },
    { id: "mix.export", title: "Export mix", group: "Actions", icon: FileOutput, combo: "Mod+E", hint: "Soon", keywords: "render audio file", run: soon({ name: "mixes" }, "Exporting a mix", "#70") },
    { id: "set.new", title: "New set", group: "Actions", icon: ListPlus, combo: "Mod+N", hint: "Soon", keywords: "create plan", run: soon({ name: "sets" }, "Set planning", "#60") },

    { id: "play.toggle", title: "Play or pause", group: "Playback", icon: Pause, combo: "Space", enabled: hasTrack, run: () => player.playPause() },
    { id: "play.stop", title: "Stop", group: "Playback", icon: Square, combo: "Mod+.", enabled: hasTrack, run: () => player.stop() },
    { id: "play.louder", title: "Volume up", group: "Playback", icon: Volume2, combo: "Mod+ArrowUp", run: () => player.setVolume(usePlayer.getState().volume + 0.1) },
    { id: "play.quieter", title: "Volume down", group: "Playback", icon: Volume1, combo: "Mod+ArrowDown", run: () => player.setVolume(usePlayer.getState().volume - 0.1) },

    { id: "app.palette", title: "Command palette", group: "App", combo: "Mod+K", allowInInputs: true, allowInModal: true, paletteHidden: true, run: ui.togglePalette },
    { id: "app.setup", title: "Run setup again", group: "App", icon: Sparkles, keywords: "first run onboarding welcome models folders", run: reopenOnboarding },
    { id: "app.shortcuts", title: "Keyboard shortcuts", group: "App", icon: Keyboard, combo: "?", allowInModal: true, keywords: "keys help", run: ui.toggleShortcuts },
  ];
}
