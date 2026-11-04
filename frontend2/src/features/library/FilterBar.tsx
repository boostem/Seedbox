/** The library's filters (#73): vibe text, BPM range, key compatible with X, energy, "not analysed yet". */
import { BookmarkPlus, Search, X } from "lucide-react";
import { useState } from "react";
import { Button, Input, Select } from "@/components/ui";
import { CAMELOT_CODES, keyColor } from "@/lib/camelot";
import { cn } from "@/lib/cn";
import { activeFilters, EMPTY_FILTER, isEmptyFilter, type LibraryFilter } from "./filter";

export interface FilterBarProps {
  filter: LibraryFilter;
  onChange: (f: LibraryFilter) => void;
  /** Save the current filter as a smart crate. */
  onSave: (name: string) => void;
}

function numOrNull(v: string): number | null {
  const n = Number(v);
  return v.trim() !== "" && Number.isFinite(n) ? n : null;
}

export function FilterBar({ filter: f, onChange, onSave }: FilterBarProps) {
  const set = (patch: Partial<LibraryFilter>) => onChange({ ...f, ...patch });
  const [naming, setNaming] = useState<string | null>(null);
  const empty = isEmptyFilter(f);
  return (
    <div className="flex flex-wrap items-center gap-2 pb-3">
      <Input
        icon={Search}
        wrapperClassName="w-72"
        placeholder="Vibe: title, artist, genre, comment…"
        value={f.text}
        onChange={(e) => set({ text: e.target.value })}
        aria-label="Search the library"
        suffix={f.text ? <X size={12} className="cursor-pointer hover:text-fg" onClick={() => set({ text: "" })} aria-label="Clear the search" /> : undefined}
      />
      <div className="flex items-center gap-1" role="group" aria-label="BPM range">
        <Input wrapperClassName="w-16" inputMode="decimal" placeholder="BPM" value={f.bpmMin ?? ""} onChange={(e) => set({ bpmMin: numOrNull(e.target.value) })} aria-label="Lowest BPM" />
        <span className="text-fg-faint">–</span>
        <Input wrapperClassName="w-16" inputMode="decimal" placeholder="BPM" value={f.bpmMax ?? ""} onChange={(e) => set({ bpmMax: numOrNull(e.target.value) })} aria-label="Highest BPM" />
        <Chip on={f.halfDouble} onClick={() => set({ halfDouble: !f.halfDouble })} title="Count half and double tempo too">
          ½× 2×
        </Chip>
      </div>
      <Select
        aria-label="Key compatible with"
        value={f.keyCompat ?? ""}
        onChange={(e) => set({ keyCompat: e.target.value || null })}
        style={f.keyCompat ? { color: keyColor(f.keyCompat).base } : undefined}
        title="Keys that mix with this one: the same, ±1 on the Camelot wheel, or its relative"
      >
        <option value="">Any key</option>
        {CAMELOT_CODES.map((c) => (
          <option key={c} value={c}>
            Fits {c}
          </option>
        ))}
      </Select>
      <div className="flex items-center gap-1" role="group" aria-label="Energy range">
        <Select aria-label="Lowest energy" value={f.energyMin ?? ""} onChange={(e) => set({ energyMin: numOrNull(e.target.value) })}>
          <option value="">Energy</option>
          {Array.from({ length: 10 }, (_, i) => (
            <option key={i} value={i + 1}>
              ≥ {i + 1}
            </option>
          ))}
        </Select>
        <Select aria-label="Highest energy" value={f.energyMax ?? ""} onChange={(e) => set({ energyMax: numOrNull(e.target.value) })}>
          <option value="">to</option>
          {Array.from({ length: 10 }, (_, i) => (
            <option key={i} value={i + 1}>
              ≤ {i + 1}
            </option>
          ))}
        </Select>
      </div>
      <Chip on={f.unanalysed} onClick={() => set({ unanalysed: !f.unanalysed })}>
        Not analysed yet
      </Chip>
      {!empty && (
        <>
          {naming === null ? (
            <Button size="sm" variant="ghost" icon={BookmarkPlus} onClick={() => setNaming(activeFilters(f).join(", "))} title="Keep this filter as a smart crate">
              Save as smart crate
            </Button>
          ) : (
            <form
              className="flex items-center gap-1"
              onSubmit={(e) => {
                e.preventDefault();
                if (naming.trim()) onSave(naming.trim());
                setNaming(null);
              }}
            >
              <Input wrapperClassName="w-56" autoFocus value={naming} onChange={(e) => setNaming(e.target.value)} onKeyDown={(e) => e.key === "Escape" && setNaming(null)} aria-label="Smart crate name" />
              <Button size="sm" type="submit" variant="primary">
                Save
              </Button>
            </form>
          )}
          <Button size="sm" variant="ghost" icon={X} onClick={() => onChange(EMPTY_FILTER)}>
            Clear
          </Button>
        </>
      )}
    </div>
  );
}

function Chip({ on, onClick, title, children }: { on: boolean; onClick: () => void; title?: string; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      title={title}
      onClick={onClick}
      className={cn(
        "h-control-md rounded-sm border px-2 text-sm whitespace-nowrap transition-colors",
        on ? "border-accent/60 bg-accent-soft text-fg" : "border-line-strong bg-raised text-fg-muted hover:border-fg-faint hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}
