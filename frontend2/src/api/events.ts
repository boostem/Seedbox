/**
 * Server-sent events: `useServerEvents()` (mounted once in <App>) keeps GET /api/events open, reconnects with
 * backoff, tracks the connection state in the app store and hands each `{type, data}` event to the listeners.
 * Screens never open their own EventSource.
 */
import { useEffect } from "react";
import { useApp } from "@/store/app";
import { openEventStream } from "./client";
import type { ServerEvent } from "./types";

const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];

const listeners = new Set<(ev: ServerEvent) => void>();
/** Listen to every server event. Returns an unsubscribe function. */
export function onServerEvent(fn: (ev: ServerEvent) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useServerEvents(): void {
  useEffect(() => {
    let stream: EventSource | null = null;
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    const { setConnection, refreshSystem } = useApp.getState();

    const scheduleReconnect = () => {
      if (disposed || timer) return;
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      setConnection(attempt > 3 ? "offline" : "reconnecting");
      timer = setTimeout(() => {
        timer = undefined;
        connect();
      }, delay);
    };

    const connect = () => {
      stream?.close();
      stream = openEventStream();
      stream.onopen = () => {
        if (attempt > 0) void refreshSystem(); // the engine may have restarted
        attempt = 0;
        setConnection("live");
      };
      stream.onmessage = (ev: MessageEvent<string>) => {
        let parsed: ServerEvent;
        try {
          parsed = JSON.parse(ev.data) as ServerEvent;
        } catch {
          return;
        }
        for (const fn of listeners) fn(parsed);
      };
      stream.onerror = () => {
        stream?.close();
        scheduleReconnect();
      };
    };

    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      stream?.close();
    };
  }, []);
}
