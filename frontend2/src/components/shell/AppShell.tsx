import { type ReactNode, useEffect } from "react";
import { registerCommands } from "@/lib/commands";
import { appCommands } from "./appCommands";
import { CommandPalette } from "./CommandPalette";
import { GlobalHotkeys } from "./GlobalHotkeys";
import { Rail } from "./Rail";
import { ShortcutsSheet } from "./ShortcutsSheet";
import { Toaster } from "./Toaster";
import { TopBar } from "./TopBar";
import { TransportStrip } from "./TransportStrip";

/** Window layout: rail | (top bar / page), the transport strip along the bottom; palette, shortcuts and toasts on top. */
export function AppShell({ children }: { children: ReactNode }) {
  useEffect(() => registerCommands(appCommands()), []);
  return (
    <div className="flex h-full flex-col">
      <GlobalHotkeys />
      <div className="flex min-h-0 flex-1">
        <Rail />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar />
          <main className="min-h-0 flex-1 overflow-hidden bg-surface">{children}</main>
        </div>
      </div>
      <TransportStrip />
      <Toaster />
      <CommandPalette />
      <ShortcutsSheet />
    </div>
  );
}
