import { Disc3, Library, ListMusic } from "lucide-react";
import { useEffect } from "react";
import { useServerEvents } from "@/api/events";
import { AppShell } from "@/components/shell/AppShell";
import { Button, EmptyState } from "@/components/ui";
import { navigate, useRoute } from "@/lib/router";
import { AreaPage } from "@/pages/AreaPage";
import { BenchPage } from "@/pages/BenchPage";
import { HomePage } from "@/pages/HomePage";
import { SystemPage } from "@/pages/SystemPage";
import { UiPage } from "@/pages/UiPage";
import { useApp } from "@/store/app";

function Routes() {
  const route = useRoute();
  switch (route.name) {
    case "home":
      return <HomePage />;
    case "library":
      return <AreaPage title="Library" icon={Library} description="Import folders and Rekordbox/iTunes XML, see analysis status (#38, #40, #73)." />;
    case "sets":
      return <AreaPage title="Sets" icon={ListMusic} description="Plan sets on Seedbox 2's own analysis, with energy arcs (#60–#64, #75)." />;
    case "mixes":
      return <AreaPage title="Mixes" icon={Disc3} description="Stem-aware transitions, the live engine and the transition editor (#51–#59, #65–#67, #79)." />;
    case "system":
      return <SystemPage />;
    case "ui":
      return <UiPage />;
    case "bench":
      return <BenchPage />;
    case "notFound":
      return (
        <EmptyState
          className="h-full"
          title="Page not found"
          description={`#/${route.path}`}
          action={
            <Button variant="primary" onClick={() => navigate({ name: "home" })}>
              Go home
            </Button>
          }
        />
      );
  }
}

export function App() {
  useServerEvents();
  useEffect(() => {
    void useApp.getState().refreshSystem();
  }, []);
  return (
    <AppShell>
      <Routes />
    </AppShell>
  );
}
