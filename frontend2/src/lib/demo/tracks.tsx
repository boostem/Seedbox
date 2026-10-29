/** Synthetic library rows and a virtualised table of them, for #/ui and the benchmark's list scenario (#/bench). */
import { memo, useMemo, useRef } from "react";
import { Table, TableScroll, TBody, Td, Th, THead, Tr, VirtualRows } from "@/components/ui";
import { fmtTime } from "@/lib/format";

export interface DemoTrack {
  id: string;
  title: string;
  artist: string;
  bpm: number;
  key: string;
  duration: number;
}

const WORDS = ["Night", "Drive", "Echo", "Signal", "Glass", "Pulse", "Velvet", "Static", "Horizon", "Ember", "Tide", "Neon", "Orbit", "Haze"];
const ARTISTS = ["Kalo", "Mira Vale", "Dust Theory", "Oberon", "Lumen", "Feral Bloom", "Ostra", "Juno Park"];

export function demoTracks(n: number): DemoTrack[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `t${i}`,
    title: `${WORDS[(i * 7) % WORDS.length]} ${WORDS[(i * 3 + 5) % WORDS.length]}`,
    artist: ARTISTS[(i * 5) % ARTISTS.length],
    bpm: 118 + ((i * 37) % 60) / 2,
    key: `${1 + ((i * 5) % 12)}${i % 3 ? "A" : "B"}`,
    duration: 150 + ((i * 53) % 240),
  }));
}

const Row = memo(function Row({ i, t }: { i: number; t: DemoTrack }) {
  return (
    <Tr>
      <Td num muted align="right">{i + 1}</Td>
      <Td truncate>{t.title}</Td>
      <Td truncate muted>{t.artist}</Td>
      <Td num align="right">{t.bpm.toFixed(1)}</Td>
      <Td num>{t.key}</Td>
      <Td num align="right" muted>{fmtTime(t.duration)}</Td>
    </Tr>
  );
});

/** `count` rows in a virtualised table; give it a height. */
export function DemoTrackTable({ count, className }: { count: number; className?: string }) {
  const tracks = useMemo(() => demoTracks(count), [count]);
  const scrollRef = useRef<HTMLDivElement>(null);
  return (
    <TableScroll ref={scrollRef} className={className}>
      <Table className="table-fixed">
        <THead>
          <Tr interactive={false}>
            <Th align="right" width="4rem">#</Th>
            <Th>Title</Th>
            <Th>Artist</Th>
            <Th align="right" width="5rem">BPM</Th>
            <Th width="4rem">Key</Th>
            <Th align="right" width="5rem">Time</Th>
          </Tr>
        </THead>
        <TBody>
          <VirtualRows scrollRef={scrollRef} count={tracks.length} colSpan={6} rowKey={(i) => tracks[i].id} row={(i) => <Row i={i} t={tracks[i]} />} />
        </TBody>
      </Table>
    </TableScroll>
  );
}
