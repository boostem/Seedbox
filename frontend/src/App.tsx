import { useEffect } from "react";
import { useServerEvents } from "@/api/events";
import { AppShell } from "@/components/shell/AppShell";
import { Button, EmptyState } from "@/components/ui";
import { APP_NAME, isSetbox, OTHER_APP_NAME } from "@/lib/appMode";
import { navigate, type Route, type RouteName, useRoute } from "@/lib/router";
import { CratePage } from "@/pages/CratePage";
import { CratesPage } from "@/pages/CratesPage";
import { DownloadsPage } from "@/pages/DownloadsPage";
import { SearchPage } from "@/pages/SearchPage";
import { SetBuilderPage } from "@/pages/SetBuilderPage";
import { SetlistPage } from "@/pages/SetlistPage";
import { SetlistsPage } from "@/pages/SetlistsPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { UiGalleryPage } from "@/pages/UiGalleryPage";
import { useApp } from "@/store/app";

/** Each app's pages: Seedbox finds, previews, downloads and tags; Setbox builds sets, mixes and setlists. */
const PAGES: Record<"seedbox" | "setbox", ReadonlySet<RouteName>> = {
  seedbox: new Set<RouteName>(["search", "crate", "downloads", "crates", "settings", "ui", "notFound"]),
  setbox: new Set<RouteName>(["crates", "set", "setlists", "setlist", "settings", "ui", "notFound"]),
};
export const HOME: Route = isSetbox ? { name: "crates" } : { name: "search" };

function Routes() {
  const route = useRoute();
  if (!PAGES[isSetbox ? "setbox" : "seedbox"].has(route.name)) {
    return (
      <EmptyState
        className="h-full"
        title={`That's in ${OTHER_APP_NAME}`}
        description={isSetbox ? "Finding, previewing and downloading songs happens in Seedbox." : "Sets, transitions, mixes and setlists live in Setbox."}
        action={
          <Button variant="primary" onClick={() => navigate(HOME)}>
            Back to {APP_NAME}
          </Button>
        }
      />
    );
  }
  switch (route.name) {
    case "search":
      return <SearchPage />;
    case "crate":
      return <CratePage key={route.seedId} seedId={route.seedId} />;
    case "downloads":
      return <DownloadsPage key={route.jobId} jobId={route.jobId} />;
    case "crates":
      return <CratesPage />;
    case "set":
      return <SetBuilderPage key={route.folder} folder={route.folder} />;
    case "setlists":
      return <SetlistsPage />;
    case "setlist":
      return <SetlistPage key={route.id} id={route.id} />;
    case "settings":
      return <SettingsPage />;
    case "ui":
      return <UiGalleryPage />;
    case "notFound":
      return (
        <EmptyState
          className="h-full"
          title="Page not found"
          description={`#/${route.path}`}
          action={
            <Button variant="primary" onClick={() => navigate(HOME)}>
              Back to {APP_NAME}
            </Button>
          }
        />
      );
  }
}

export function App() {
  useServerEvents();
  useEffect(() => {
    document.title = APP_NAME;
    if (!window.location.hash) navigate(HOME, { replace: true });
    void useApp.getState().refreshBasics();
  }, []);
  return (
    <AppShell>
      <Routes />
    </AppShell>
  );
}
