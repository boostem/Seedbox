import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { KeyStatus } from "@/api/types";
import { Button, CamelotWheel, InfoRow, KeyBadge, Modal, Switch } from "@/components/ui";
import { formatKey } from "@/lib/camelot";
import { fmtTime } from "@/lib/format";
import { refreshSoon } from "@/store/library";
import { ui, useUi } from "@/store/ui";

const SOURCE: Record<string, string> = { rekordbox: "Rekordbox", tunebat: "Tunebat", file: "File tags", itunes: "iTunes" };

/** A track's key (#45): what our analysis says and how sure it is, the tags that disagree, and the user's own key. */
export function KeyEditor() {
  const hash = useUi((s) => s.keyEditor);
  const notation = useUi((s) => s.keyNotation);
  const [status, setStatus] = useState<KeyStatus | null>(null);
  useEffect(() => {
    setStatus(null);
    if (!hash) return;
    let live = true;
    void api.key.status(hash, { quiet: true }).then(
      (s) => {
        if (live && "effective" in s) setStatus(s);
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [hash]);
  const apply = (request: Promise<KeyStatus>) => {
    void request.then((s) => {
      setStatus(s);
      if (hash) refreshSoon(hash);
    });
  };
  const analysis = status?.analysis;
  return (
    <Modal
      open={!!hash}
      onClose={() => ui.editKey(null)}
      title="Key"
      footer={
        status?.effective.from === "user" ? (
          <Button variant="secondary" onClick={() => hash && apply(api.key.clearOverride(hash))}>
            Use our analysis
          </Button>
        ) : undefined
      }
    >
      <div className="flex gap-5">
        <CamelotWheel
          size={200}
          active={status?.effective.camelot ?? null}
          center={<KeyBadge camelot={status?.effective.camelot} />}
          onSelect={(code) => hash && apply(api.key.override(hash, code))}
        />
        <div className="min-w-0 flex-1 text-sm">
          {status ? (
            <>
              {status.effective.from === "user" && <InfoRow label="Your key">{status.effective.key}</InfoRow>}
              {analysis ? (
                <>
                  <InfoRow label="Our analysis">{analysis.key ?? (analysis.best_guess ? `Not sure (${analysis.best_guess.key}?)` : "No key found")}</InfoRow>
                  <InfoRow label="Confidence">{`${Math.round(analysis.confidence * 100)} %`}</InfoRow>
                  <InfoRow label="From">{`${analysis.source === "stems" ? "Bass and other stems" : "The mix"}${analysis.tuning_cents ? `, tuned ${analysis.tuning_cents > 0 ? "+" : ""}${analysis.tuning_cents} cents` : ""}`}</InfoRow>
                </>
              ) : (
                <InfoRow label="Our analysis">Not done yet</InfoRow>
              )}
              {analysis?.changes.map((c) => (
                <InfoRow key={c.start_s} label={`${fmtTime(c.start_s)}–${fmtTime(c.end_s)}`}>{`${c.key} (${formatKey(c.camelot, notation)})`}</InfoRow>
              ))}
              {status.hints.map((h) => (
                <InfoRow key={h.key} label={SOURCE[h.from] ?? h.from}>{formatKey(h.key, notation)}</InfoRow>
              ))}
            </>
          ) : (
            <p className="text-fg-muted">Analysing…</p>
          )}
          <p className="px-4 pt-3 text-fg-muted">Pick a key on the wheel to set your own. It stays through new analyses.</p>
          <div className="px-4 pt-3">
            <Switch checked={notation === "open"} onChange={(on) => ui.setKeyNotation(on ? "open" : "camelot")} label="Show keys as Open Key" />
          </div>
        </div>
      </div>
    </Modal>
  );
}
