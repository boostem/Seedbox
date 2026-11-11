import { lazy, Suspense, useEffect, useRef } from "react";
import { useServerEvents } from "@/api/events";
import { AppShell } from "@/components/shell/AppShell";
import { Button, EmptyState } from "@/components/ui";
import { Onboarding } from "@/features/onboarding/Onboarding";
import { cn } from "@/lib/cn";
import { navigate, type Route, useRoute } from "@/lib/router";
import { BenchPage } from "@/pages/BenchPage";
import { CratesPage } from "@/pages/CratesPage";
import { DiscoverPage } from "@/pages/DiscoverPage";
import { HomePage } from "@/pages/HomePage";
import { LibraryPage } from "@/pages/LibraryPage";
import { ListenPage } from "@/pages/ListenPage";
import { MixesPage } from "@/pages/MixesPage";
import { SetsPage } from "@/pages/SetsPage";
import { SystemPage } from "@/pages/SystemPage";
import { UiPage } from "@/pages/UiPage";
import { useApp } from "@/store/app";
import { followImports } from "@/store/imports";
import { followModels } from "@/store/models";
import { followJobs } from "@/store/jobs";
import { followPower } from "@/store/power";
import { loadOnboarding, useOnboarding } from "@/store/onboarding";

// the live engine and the decks load when someone opens them
const NowPlayingPage = lazy(() => import("@/pages/NowPlayingPage").then((m) => ({ default: m.NowPlayingPage })));

function Page({ route }: { route: Route }) {
  switch (route.name) {
    case "home":
      return <HomePage />;
    case "library":
      return <LibraryPage />;
    case "crates":
      return <CratesPage />;
    case "sets":
      return <SetsPage />;
    case "discover":
      return <DiscoverPage />;
    case "mixes":
      return <MixesPage />;
    case "settings":
      return <SystemPage />;
    case "ui":
      return <UiPage />;
    case "bench":
      return <BenchPage />;
    case "listen":
      return <ListenPage />;
    case "nowPlaying":
      return null; // App shows it, without the shell
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
  useEffect(followPower, []);
  useEffect(() => void loadOnboarding(), []);
  const firstRun = useOnboarding((s) => s.open);
  const route = useRoute();
  if (route.name === "nowPlaying") {
    return (
      <Suspense fallback={null}>
        <NowPlayingPage />
      </Suspense>
    );
  }
  if (firstRun) return <Onboarding />;
  return (
    <AppShell>
      <Routes />
    </AppShell>
  );
}
