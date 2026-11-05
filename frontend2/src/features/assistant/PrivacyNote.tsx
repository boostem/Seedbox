/** What the assistant sends, in the one place that says so (#63). `seedbox2/assistant/tools.py` is what holds it true. */
export function PrivacyNote({ className }: { className?: string }) {
  return (
    <div className={className}>
      <p>
        <strong className="font-medium text-fg">What goes to Anthropic:</strong> what you type, and for the tracks you allow: titles, artists, genre tags and our analysis numbers (length, tempo, key, energy, how much is sung).
      </p>
      <p className="mt-1">
        <strong className="font-medium text-fg">Nothing else:</strong> no audio, no files or folder names, no embeddings, no names of your crates or sets. Everything but the assistant works without it and without a network. Each run is billed to your API key.
      </p>
    </div>
  );
}
