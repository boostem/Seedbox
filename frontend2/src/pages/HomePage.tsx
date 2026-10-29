import { Disc3, Library, ListMusic } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Page, PageBody, PageHeader, Panel } from "@/components/ui";
import { Link, type Route } from "@/lib/router";
import { useApp } from "@/store/app";

function AreaCard({ to, icon: Icon, title, text }: { to: Route; icon: LucideIcon; title: string; text: string }) {
  return (
    <Link to={to} className="block">
      <Panel className="h-full p-4 transition-colors hover:border-line-strong hover:bg-raised/40">
        <Icon size={18} className="text-accent-hover" />
        <div className="mt-3 text-md font-semibold">{title}</div>
        <p className="mt-1 text-sm text-fg-subtle">{text}</p>
      </Panel>
    </Link>
  );
}

export function HomePage() {
  const system = useApp((s) => s.system);
  return (
    <Page>
      <PageHeader title="Seedbox 2" subtitle={system ? `v${system.version} · ${system.platform} ${system.machine}` : "Starting…"} />
      <PageBody>
        <div className="grid max-w-[880px] grid-cols-3 gap-3">
          <AreaCard to={{ name: "library" }} icon={Library} title="Library" text="Your tracks, analysed once and kept by content hash." />
          <AreaCard to={{ name: "sets" }} icon={ListMusic} title="Sets" text="Order tracks along an energy arc." />
          <AreaCard to={{ name: "mixes" }} icon={Disc3} title="Mixes" text="Stem-aware transitions you can play, edit and render." />
        </div>
      </PageBody>
    </Page>
  );
}
