import type { LucideIcon } from "lucide-react";
import { EmptyState, Page, PageBody, PageHeader } from "@/components/ui";

/** Placeholder for an area whose tickets haven't landed yet. */
export function AreaPage({ title, icon, description }: { title: string; icon: LucideIcon; description: string }) {
  return (
    <Page>
      <PageHeader title={title} />
      <PageBody>
        <EmptyState icon={icon} title={`${title} is coming`} description={description} />
      </PageBody>
    </Page>
  );
}
