import { useEffect } from "react";
import { useServerEvents } from "@/api/events";
import { AppShell } from "@/components/shell/AppShell";
import { Button, EmptyState } from "@/components/ui";
import { navigate, useRoute } from "@/lib/router";
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

function Routes() {
  const route = useRoute();
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
            <Button variant="primary" onClick={() => navigate({ name: "search" })}>
              Go to Search
            </Button>
          }
        />
      );
  }
}

export function App() {
  useServerEvents();
  useEffect(() => {
    if (!window.location.hash) navigate({ name: "search" }, { replace: true });
    void useApp.getState().refreshBasics();
  }, []);
  return (
    <AppShell>
      <Routes />
    </AppShell>
  );
}
