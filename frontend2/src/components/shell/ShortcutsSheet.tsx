import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { IconButton, Kbd } from "@/components/ui";
import { commandsByGroup, useCommands } from "@/lib/commands";
import { usePresence } from "@/lib/usePresence";
import { ui, useUi } from "@/store/ui";

/** Keys that aren't commands: they work inside the palette, lists and dialogs. */
const OTHER_KEYS: { label: string; keys: string[] }[] = [
  { label: "Move in the palette", keys: ["↑", "↓"] },
  { label: "Run the highlighted command", keys: ["↵"] },
  { label: "Close a dialog or the palette", keys: ["Esc"] },
  { label: "Move focus", keys: ["Tab", "⇧Tab"] },
];

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex h-7 items-center gap-3 text-base">
      <span className="min-w-0 flex-1 truncate text-fg-muted">{label}</span>
      <span className="flex shrink-0 items-center gap-1">{children}</span>
    </div>
  );
}

function Sheet({ state, onAnimationEnd }: { state: "open" | "closed"; onAnimationEnd: ReturnType<typeof usePresence>["onAnimationEnd"] }) {
  const ref = useRef<HTMLDialogElement>(null);
  const commands = useCommands((s) => s.commands);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      data-state={state}
      aria-labelledby="shortcuts-title"
      onAnimationEnd={onAnimationEnd}
      onCancel={(e) => {
        e.preventDefault();
        ui.closeShortcuts();
      }}
      onMouseDown={(e) => {
        if (e.target === ref.current) ui.closeShortcuts();
      }}
      className="fixed inset-y-0 -right-5 left-auto m-0 h-full max-h-none w-md max-w-[92vw] bg-overlay p-0 pr-5 text-fg shadow-modal data-[state=closed]:animate-panel-out data-[state=open]:animate-panel-in"
    >
      <div className="flex h-full flex-col">
        <header className="flex h-11 shrink-0 items-center gap-3 border-b border-line pr-2 pl-4">
          <h2 id="shortcuts-title" className="flex-1 text-md font-semibold">Keyboard shortcuts</h2>
          <IconButton icon={X} label="Close" size="sm" tooltip={false} onClick={ui.closeShortcuts} />
        </header>
        {/* focused on open, so ↑↓ scroll the list (the close button would get the first focus otherwise) */}
        <div autoFocus tabIndex={-1} className="min-h-0 flex-1 overflow-y-auto px-4 py-3 outline-none scroll-thin">
          {commandsByGroup(commands).map(({ group, commands }) => (
            <section key={group} className="mb-4">
              <h3 className="mb-1 label-caps">{group}</h3>
              {commands.map((c) => (
                <Line key={c.id} label={c.title}>
                  {c.combo ? <Kbd combo={c.combo} subtle /> : <span className="text-xs text-fg-faint">in <Kbd combo="Mod+K" subtle /></span>}
                </Line>
              ))}
            </section>
          ))}
          <section>
            <h3 className="mb-1 label-caps">Everywhere</h3>
            {OTHER_KEYS.map((k) => (
              <Line key={k.label} label={k.label}>
                {k.keys.map((key) => <Kbd key={key} subtle>{key}</Kbd>)}
              </Line>
            ))}
          </section>
        </div>
        <p className="shrink-0 border-t border-line px-4 py-2.5 text-xs text-fg-subtle">Every command is also in the palette: <Kbd combo="Mod+K" subtle /></p>
      </div>
    </dialog>
  );
}

/** ?: every shortcut, from the command registry. Slides in from the right edge with the spring. */
export function ShortcutsSheet() {
  const open = useUi((s) => s.shortcutsOpen);
  const p = usePresence(open);
  return p.mounted ? <Sheet state={p.state} onAnimationEnd={p.onAnimationEnd} /> : null;
}
