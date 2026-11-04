import { Boxes, Compass, Disc3, ListMusic } from "lucide-react";
import { useEffect, useRef } from "react";
import { useServerEvents } from "@/api/events";
import { AppShell } from "@/components/shell/AppShell";
import { Button, EmptyState } from "@/components/ui";
import { Onboarding } from "@/features/onboarding/Onboarding";
import { cn } from "@/lib/cn";
import { navigate, type Route, useRoute } from "@/lib/router";
import { AreaPage } from "@/pages/AreaPage";
import { BenchPage } from "@/pages/BenchPage";
import { HomePage } from "@/pages/HomePage";
import { LibraryPage } from "@/pages/LibraryPage";
import { ListenPage } from "@/pages/ListenPage";
import { SystemPage } from "@/pages/SystemPage";
import { UiPage } from "@/pages/UiPage";
import { useApp } from "@/store/app";
import { followImports } from "@/store/imports";
import { followModels } from "@/store/models";
import { followJobs } from "@/store/jobs";
import { loadOnboarding, useOnboarding } from "@/store/onboarding";

function Page({ route }: { route: Route }) {
  switch (route.name) {
    case "home":
      return <HomePage />;
    case "library":
      return <LibraryPage />;
    case "crates":
      return <AreaPage title="Crates" icon={Boxes} description="Group tracks into crates, and plan sets across them (#40, #62)." />;
    case "sets":
      return <AreaPage title="Sets" icon={ListMusic} description="Plan sets on Seedbox 2's own analysis, with energy arcs (#60–#64, #75)." />;
    case "discover":
      return <AreaPage title="Discover" icon={Compass} description="Tracks that fit a gap in a set, by key, energy and vibe (#48, #64)." />;
    case "mixes":
      return <AreaPage title="Mixes" icon={Disc3} description="Stem-aware transitions, the live engine and the transition editor (#51–#59, #65–#67, #79)." />;
    case "settings":
      return <SystemPage />;
    case "ui":
      return <UiPage />;
    case "bench":
      return <BenchPage />;
    case "listen":
      return <ListenPage />;
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

/** The page for the route. A new page rises in (animate-route-in); the first one after launch just appears. */
function Routes() {
  const route = useRoute();
  const key = route.name === "notFound" ? `notFound:${route.path}` : route.name;
  const first = useRef(key);
  const moved = useRef(false);
  if (key !== first.current) moved.current = true;
  return (
    <div key={key} className={cn("h-full", moved.current && "animate-route-in")}>
      <Page route={route} />
    </div>
  );
}

export function App() {
  useServerEvents();
  useEffect(() => {
    void useApp.getState().refreshSystem();
  }, []);
  useEffect(followImports, []);
  useEffect(followModels, []);
  useEffect(followJobs, []);
  useEffect(() => void loadOnboarding(), []);
  const firstRun = useOnboarding((s) => s.open);
  if (firstRun) return <Onboarding />;
  return (
    <AppShell>
      <Routes />
    </AppShell>
  );
}
