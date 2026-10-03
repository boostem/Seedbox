/** Pages can refine the top-bar breadcrumb: usePageTitle("Set builder", folderName). */
import { useEffect } from "react";
import { create } from "zustand";

export const usePageTitleStore = create<{ parts: string[] | null }>()(() => ({ parts: null }));

export function usePageTitle(...parts: (string | null | undefined)[]): void {
  const key = parts.filter(Boolean).join("\u0000");
  useEffect(() => {
    usePageTitleStore.setState({ parts: key ? key.split("\u0000") : null });
    return () => usePageTitleStore.setState({ parts: null });
  }, [key]);
}
