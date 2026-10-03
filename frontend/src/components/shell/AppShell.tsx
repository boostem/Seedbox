import type { ReactNode } from "react";
import { GlobalHotkeys } from "./GlobalHotkeys";
import { MiniPlayer } from "./MiniPlayer";
import { Sidebar } from "./Sidebar";
import { Toaster } from "./Toaster";
import { TopBar } from "./TopBar";

/** Window layout: sidebar | (top bar / page), mini player along the bottom, toasts on top. */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col">
      <GlobalHotkeys />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar />
          <main className="min-h-0 flex-1 overflow-hidden bg-surface">{children}</main>
        </div>
      </div>
      <MiniPlayer />
      <Toaster />
    </div>
  );
}
