import { type Command, isEnabled, useCommands } from "@/lib/commands";
import { isActivationTarget, matchesCombo, useHotkey } from "@/lib/hotkeys";

/** Binds the combos of one kind of command (whether they also fire while typing / over a dialog). */
function Bind({ commands, allowInInputs, allowInModal }: { commands: Command[]; allowInInputs: boolean; allowInModal: boolean }) {
  const mine = commands.filter((c) => c.combo && !!c.allowInInputs === allowInInputs && !!c.allowInModal === allowInModal);
  const combos = (c: Command) => [c.combo!, ...(c.altCombos ?? [])];
  useHotkey(
    mine.flatMap(combos),
    (e) => {
      // Space on a focused button or link presses it; it only plays when nothing else wants it
      if (e.code === "Space" && isActivationTarget(e.target)) return false;
      const c = mine.find((c) => combos(c).some((k) => matchesCombo(e, k)) && isEnabled(c));
      if (!c) return false;
      e.preventDefault();
      c.run();
    },
    { allowInInputs, allowInModal, preventDefault: false, enabled: mine.length > 0 },
  );
  return null;
}

/** Every registered command's shortcut (lib/commands.ts). Pages can still add their own with useHotkey. */
export function GlobalHotkeys() {
  const commands = useCommands((s) => s.commands);
  return (
    <>
      <Bind commands={commands} allowInInputs={false} allowInModal={false} />
      <Bind commands={commands} allowInInputs={false} allowInModal />
      <Bind commands={commands} allowInInputs allowInModal={false} />
      <Bind commands={commands} allowInInputs allowInModal />
    </>
  );
}
