// Regenerates src/mock/fixtures/tunebat.json from the captured tunebat responses in the project root.
// Run: deno task fixtures   (only needed if ../tracks.txt or ../search.txt change)
// Conversion mirrors musicreco/models.py Track.from_api (+ the derived fields the API serialises).

type Raw = Record<string, unknown>;

function body(path: string): Raw {
  const text = Deno.readTextFileSync(path);
  const start = text.indexOf('{"data"');
  if (start < 0) throw new Error(`${path}: no JSON body`);
  const json = text.slice(start, text.lastIndexOf("}") + 1);
  return (JSON.parse(json) as { data: Raw }).data;
}

function track(d: Raw) {
  const images = [...((d.ci as { iu: string; iw?: number }[] | null) ?? [])].sort((a, b) => (b.iw ?? 0) - (a.iw ?? 0));
  const artists = [...((d.as as string[] | null) ?? [])];
  const name = (d.n as string) || "";
  return {
    id: d.id as string,
    name,
    artists,
    display_name: `${artists.join(", ")} - ${name}`,
    album: (d.an as string) || "",
    release_date: (d.rd as string) || "",
    duration_s: Number(d.d || 0) / 1000,
    popularity: Number(d.p || 0),
    key: (d.k as string) || "",
    camelot: (d.c as string) || "",
    bpm: Number(d.b || 0),
    energy: Number(d.e || 0),
    danceability: Number(d.da || 0),
    cover_url: images[0]?.iu ?? null,
    spotify_url: `https://open.spotify.com/track/${d.id}`,
  };
}

const root = new URL("../../", import.meta.url).pathname;
const detail = body(`${root}tracks.txt`);
const search = body(`${root}search.txt`);
const out = {
  source: "tracks.txt (GET /api/tracks?trackId=44wR4NqR75eJMETxrz59Sr) + search.txt (term 'fisher favour')",
  search: {
    page: Number(search.page),
    total_count: Number(search.totalCount),
    items: (search.items as Raw[]).map(track),
  },
  seed: track(detail),
  recs: (detail.r as Raw[]).map(track),
};
const dest = new URL("../src/mock/fixtures/tunebat.json", import.meta.url).pathname;
Deno.mkdirSync(new URL("../src/mock/fixtures/", import.meta.url).pathname, { recursive: true });
Deno.writeTextFileSync(dest, JSON.stringify(out, null, 1) + "\n");
console.log(`wrote ${dest}: ${out.search.items.length} search items, seed + ${out.recs.length} recs`);
