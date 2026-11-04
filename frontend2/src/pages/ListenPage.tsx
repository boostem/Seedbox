import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ListenStatus } from "@/api/types";
import { Field, Input, Page, PageBody, PageHeader, Tabs } from "@/components/ui";
import { loadRater, saveRater } from "@/features/listen/rating";
import { ResultsPanel } from "@/features/listen/ResultsPanel";
import { TrialPanel } from "@/features/listen/TrialPanel";
import { navigate, useHashQuery } from "@/lib/router";

type Tab = "test" | "results";

/** #/listen: the blind A/B test (#86). Hidden: no rail entry and not in ⌘K; share the address with the people rating. */
export function ListenPage() {
  const query = useHashQuery();
  const tab: Tab = query.get("tab") === "results" ? "results" : "test";
  const [rater, setRater] = useState(loadRater);
  const [status, setStatus] = useState<ListenStatus | null>(null);
  const refresh = useCallback(() => {
    api.listen.status(loadRater(), { quiet: true }).then(setStatus, () => {});
  }, []);
  useEffect(refresh, [refresh, tab]);

  return (
    <Page>
      <PageHeader
        title="Listening test"
        subtitle={status ? `${status.pairs} pair${status.pairs === 1 ? "" : "s"}, ${status.progress.done} of ${status.progress.total} comparisons done by you` : "Blind A/B test of rendered transitions"}
        actions={
          <Field label="Your name" className="w-48">
            <Input
              value={rater}
              maxLength={200}
              placeholder="Anonymous"
              onChange={(e) => {
                setRater(e.target.value);
                saveRater(e.target.value);
              }}
            />
          </Field>
        }
      >
        <Tabs<Tab>
          items={[{ id: "test", label: "Test" }, { id: "results", label: "Results", title: "Rate first: the results can bias you" }]}
          value={tab}
          onChange={(t) => navigate(t === "results" ? "#/listen?tab=results" : "#/listen", { replace: true })}
        />
      </PageHeader>
      <PageBody>
        {tab === "test" ? <TrialPanel rater={rater} folder={status?.folder} onRated={refresh} /> : <ResultsPanel rater={rater} onChange={refresh} />}
      </PageBody>
    </Page>
  );
}
