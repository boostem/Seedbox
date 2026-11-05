import { Disc3 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type SavedPlan } from "@/api/client";
import { Page, PageBody, PageHeader, Panel, Select } from "@/components/ui";
import { LivePlanPanel } from "@/features/mix/LivePlanPanel";
import { AreaPage } from "./AreaPage";

const keyOf = (p: SavedPlan) => `${p.from_hash}/${p.to_hash}`;

/** The saved plans, on the live engine. Until there are any it's the area's placeholder. */
export function MixesPage() {
  const [plans, setPlans] = useState<SavedPlan[] | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    api.mixes.plans({ signal: ctl.signal }).then(
      (list) => setPlans(Array.isArray(list) ? list : []),
      () => !ctl.signal.aborted && setPlans([]),
    );
    return () => ctl.abort();
  }, []);

  if (!plans?.length) {
    return <AreaPage title="Mixes" icon={Disc3} description="Stem-aware transitions, the live engine and the transition editor (#51–#59, #65–#67, #79)." />;
  }
  const current = plans.find((p) => keyOf(p) === picked) ?? plans[0];
  return (
    <Page>
      <PageHeader title="Mixes" />
      <PageBody>
        <Panel>
          <div className="border-b border-line p-3">
            <Select aria-label="Transition" className="w-full max-w-xl" value={keyOf(current)} onChange={(e) => setPicked(e.target.value)}>
              {plans.map((p) => (
                <option key={keyOf(p)} value={keyOf(p)}>
                  {p.from_name} → {p.to_name}
                </option>
              ))}
            </Select>
          </div>
          <LivePlanPanel from={current.from_hash} to={current.to_hash} />
        </Panel>
      </PageBody>
    </Page>
  );
}
