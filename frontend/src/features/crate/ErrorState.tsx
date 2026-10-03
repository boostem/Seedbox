import { CloudOff, RotateCw, SearchX, TriangleAlert, Unplug } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "@/api/client";
import { Button, EmptyState } from "@/components/ui";

export interface FriendlyError {
  icon: typeof CloudOff;
  title: string;
  description: ReactNode;
  retryable: boolean;
}

/** Turns an ApiError into DJ-friendly copy (502 = tunebat trouble, 0 = engine down, 404 = gone). */
export function describeError(error: unknown, what = "this"): FriendlyError {
  const e = error instanceof ApiError ? error : null;
  const msg = (error as Error | null)?.message ?? String(error);
  if (e?.isNetwork) {
    return { icon: Unplug, title: "Can't reach the Seedbox engine", description: "The app's engine isn't responding. It reconnects on its own, so try again in a moment.", retryable: true };
  }
  if (e?.status === 502) {
    const limited = /429|rate|too many/i.test(msg);
    return {
      icon: CloudOff,
      title: limited ? "tunebat is rate-limiting us" : "tunebat didn't answer",
      description: (
        <>
          {limited ? "Too many requests in a short time. Wait a few seconds, then retry." : "tunebat is slow or briefly unavailable. This usually clears up on a retry."}
          <span className="mt-1.5 block font-mono text-2xs text-fg-faint select-text">{msg}</span>
        </>
      ),
      retryable: true,
    };
  }
  if (e?.isNotFound) {
    return { icon: SearchX, title: `Couldn't find ${what}`, description: <span className="select-text">{msg}</span>, retryable: false };
  }
  return { icon: TriangleAlert, title: "Something went wrong", description: <span className="select-text">{msg}</span>, retryable: true };
}

export function ErrorState({ error, onRetry, what, action, className }: { error: unknown; onRetry?: () => void; what?: string; action?: ReactNode; className?: string }) {
  const f = describeError(error, what);
  return (
    <EmptyState
      className={className}
      icon={f.icon}
      title={f.title}
      description={f.description}
      action={
        <>
          {onRetry && (
            <Button variant={f.retryable ? "primary" : "secondary"} icon={RotateCw} onClick={onRetry}>
              Try again
            </Button>
          )}
          {action}
        </>
      }
    />
  );
}
