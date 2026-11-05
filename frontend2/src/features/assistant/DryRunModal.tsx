import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { AssistantDryRun, AssistantScope } from "@/api/types";
import { Modal, Skeleton } from "@/components/ui";
import { PrivacyNote } from "./PrivacyNote";

export interface DryRunModalProps {
  open: boolean;
  onClose: () => void;
  prompt: string;
  scope: AssistantScope;
}

const pretty = (v: unknown) => JSON.stringify(v, null, 2);

/** The request a run would send first, to the letter, and what a search would send back. Nothing leaves the computer. */
export function DryRunModal({ open, onClose, prompt, scope }: DryRunModalProps) {
  const [data, setData] = useState<AssistantDryRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const ctrl = new AbortController();
    setData(null);
    setError(null);
    api.assistant.dryRun({ prompt, ...scope }, { quiet: true, signal: ctrl.signal }).then(setData, (e) => {
      if (!ctrl.signal.aborted) setError((e as Error).message);
    });
    return () => ctrl.abort();
  }, [open, prompt, scope]);

  return (
    <Modal open={open} onClose={onClose} title="What would be sent" description="A dry run: this shows the request and sends nothing." size="xl">
      <div className="flex flex-col gap-3 text-sm text-fg-muted">
        <PrivacyNote />
        {error && <p className="text-danger">{error}</p>}
        {!data && !error && <Skeleton className="h-64" />}
        {data && (
          <>
            <section>
              <h3 className="mb-1 text-xs font-medium text-fg">
                POST {data.url}
              </h3>
              <pre className="max-h-64 overflow-auto rounded-sm border border-line bg-raised p-2 font-mono text-xs">{pretty({ headers: data.headers, ...data.body })}</pre>
            </section>
            <section>
              <h3 className="mb-1 text-xs font-medium text-fg">What a search sends back, with your first tracks</h3>
              <pre className="max-h-48 overflow-auto rounded-sm border border-line bg-raised p-2 font-mono text-xs">{pretty(data.example_search_result)}</pre>
            </section>
          </>
        )}
      </div>
    </Modal>
  );
}
