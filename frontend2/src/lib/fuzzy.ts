/**
 * Fuzzy matching for the command palette. `fuzzyMatch("lib", "Go to Library")` finds the query's characters in
 * order and scores how well they sit: word starts and runs of consecutive characters score high, gaps cost a little.
 * Words in the query match independently, in any order ("set new" finds "New set"). Case-insensitive.
 */

export interface FuzzyMatch {
  score: number;
  /** Matched character positions in the text, ascending (for highlighting). */
  indices: number[];
}

const MATCH = 1;
const WORD_START = 8;
const CONSECUTIVE = 6;
const GAP = 0.25; // per skipped character
const MAX_GAP = 3;

function isWordStart(text: string, i: number): boolean {
  if (i === 0) return true;
  const prev = text[i - 1];
  const ch = text[i];
  if (/[\s\-_/.:·,()&]/.test(prev)) return true;
  return prev === prev.toLowerCase() && ch !== ch.toLowerCase(); // camelCase
}

/** Best placement of one word (no spaces) in `text`, or null if its characters aren't all there in order. */
function matchWord(word: string, text: string, lower: string): FuzzyMatch | null {
  const m = word.length;
  const n = text.length;
  if (m === 0) return { score: 0, indices: [] };
  if (m > n) return null;
  // best[i][j]: best score with word[i] matched at text[j]; from[i][j]: where word[i-1] was matched
  const best: number[][] = [];
  const from: number[][] = [];
  for (let i = 0; i < m; i++) {
    best.push(new Array<number>(n).fill(-Infinity));
    from.push(new Array<number>(n).fill(-1));
    for (let j = i; j < n; j++) {
      if (lower[j] !== word[i]) continue;
      const bonus = MATCH + (isWordStart(text, j) ? WORD_START : 0);
      if (i === 0) {
        best[i][j] = bonus - Math.min(j, 10) * 0.1; // earlier is a little better
        continue;
      }
      for (let k = i - 1; k < j; k++) {
        const prev = best[i - 1][k];
        if (prev === -Infinity) continue;
        const gap = j - k - 1;
        const s = prev + bonus + (gap === 0 ? CONSECUTIVE : -Math.min(gap * GAP, MAX_GAP));
        if (s > best[i][j]) {
          best[i][j] = s;
          from[i][j] = k;
        }
      }
    }
  }
  let end = -1;
  for (let j = 0; j < n; j++) if (best[m - 1][j] > (end < 0 ? -Infinity : best[m - 1][end])) end = j;
  if (end < 0 || best[m - 1][end] === -Infinity) return null;
  const indices: number[] = [];
  for (let i = m - 1, j = end; i >= 0; j = from[i][j], i--) indices.push(j);
  return { score: best[m - 1][end], indices: indices.reverse() };
}

/** Match `query` against `text`; null when some word of the query isn't in it. An empty query matches with score 0. */
export function fuzzyMatch(query: string, text: string): FuzzyMatch | null {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const lower = text.toLowerCase();
  let score = 0;
  const indices = new Set<number>();
  for (const w of words) {
    const m = matchWord(w, text, lower);
    if (!m) return null;
    score += m.score;
    for (const i of m.indices) indices.add(i);
  }
  if (words.length && lower.startsWith(query.trim().toLowerCase())) score += WORD_START; // a prefix wins ties
  return { score, indices: [...indices].sort((a, b) => a - b) };
}

/** Every word of `query` starts a word of `text` ("ren aud" in "render audio"). */
function prefixesWords(query: string, text: string): boolean {
  const words = text.toLowerCase().split(/[\s\-_/]+/).filter(Boolean);
  return query.toLowerCase().split(/\s+/).filter(Boolean).every((q) => words.some((w) => w.startsWith(q)));
}

const KEYWORD_SCORE = 2; // below any real title match that starts a word

/**
 * Rank `items` by how well `query` matches `text(item)`, best first; items that don't match are dropped. Ties keep the
 * input order. `keywords(item)` are extra words: an item whose title doesn't match still shows, low and without
 * highlighting, when every query word starts one of its words or keywords ("render" finds "Export mix").
 */
export function fuzzyRank<T>(
  query: string,
  items: readonly T[],
  text: (item: T) => string,
  keywords?: (item: T) => string | undefined,
): { item: T; match: FuzzyMatch }[] {
  const out: { item: T; match: FuzzyMatch; order: number }[] = [];
  items.forEach((item, order) => {
    let match = fuzzyMatch(query, text(item));
    const kw = keywords?.(item);
    if (!match && kw && prefixesWords(query, `${text(item)} ${kw}`)) match = { score: KEYWORD_SCORE, indices: [] };
    if (match) out.push({ item, match, order });
  });
  out.sort((a, b) => b.match.score - a.match.score || a.order - b.order);
  return out.map(({ item, match }) => ({ item, match }));
}
