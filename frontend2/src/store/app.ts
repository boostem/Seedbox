/** App-wide state: the engine connection and what /api/system says about it. Pages keep their own state. */
import { create } from "zustand";
import { api } from "@/api/client";
import type { SystemInfo } from "@/api/types";

export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline";

interface AppState {
  connection: ConnectionState;
  system: SystemInfo | null;
  setConnection: (c: ConnectionState) => void;
  refreshSystem: () => Promise<void>;
}

export const useApp = create<AppState>()((set) => ({
  connection: "connecting",
  system: null,
  setConnection: (connection) => set({ connection }),
  refreshSystem: async () => {
    try {
      set({ system: await api.system({ quiet: true }) });
    } catch {
      /* the connection indicator shows it */
    }
  },
}));
