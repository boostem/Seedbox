/**
 * Server-sent events: `useServerEvents()` (mounted once in <App>) keeps GET /api/events open, reconnects with
 * backoff, parses each `data:` line as a ServerEvent and dispatches it to the app store (useApp.handleEvent).
 * Screens never open their own EventSource — they read the store (previews, jobs, analysis, setVersion, epoch).
 */
import { useEffect } from "react";
import { useApp } from "@/store/app";
import { type EventStream, openEventStream } from "./client";
import type { ServerEvent } from "./types";

const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];
const CLOSED = 2;

/** Extra listeners (rarely needed — prefer the store). Returns an unsubscribe function. */
const listeners = new Set<(ev: ServerEvent) => void>();
export function onServerEvent(fn: (ev: ServerEvent) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useServerEvents(): void {
  useEffect(() => {
    let stream: EventStream | null = null;
    let attempt = 0;
    let softErrors = 0; // errors while the browser retries by itself (engine down → connection refused every ~3 s)
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    const { setConnection, bumpEpoch, handleEvent, refreshBasics } = useApp.getState();

    const scheduleReconnect = () => {
      if (disposed || timer) return;
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      setConnection(attempt > 3 ? "offline" : "reconnecting");
      timer = setTimeout(() => {
        timer = undefined;
        void connect();
      }, delay);
    };

    const connect = async () => {
      stream?.close();
      try {
        stream = await openEventStream();
      } catch {
        scheduleReconnect();
        return;
      }
      if (disposed) {
        stream.close();
        return;
      }
      stream.onopen = () => {
        const wasReconnect = useApp.getState().epoch > 0;
        attempt = 0;
        softErrors = 0;
        setConnection("live");
        bumpEpoch();
        if (wasReconnect) void refreshBasics(); // resync after a gap
      };
      stream.onmessage = (msg) => {
        let ev: ServerEvent;
        try {
          ev = JSON.parse(msg.data) as ServerEvent;
        } catch {
          return;
        }
        handleEvent(ev);
        for (const fn of listeners) fn(ev);
      };
      stream.onerror = () => {
        // EventSource retries by itself while CONNECTING; once CLOSED we reconnect with backoff.
        if (stream && stream.readyState === CLOSED) {
          stream.close();
          scheduleReconnect();
        } else {
          // the native retries never reach scheduleReconnect, so escalate here too: ~10 s without the engine = offline
          softErrors += 1;
          setConnection(softErrors > 3 ? "offline" : "reconnecting");
        }
      };
    };

    void connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      stream?.close();
    };
  }, []);
}
