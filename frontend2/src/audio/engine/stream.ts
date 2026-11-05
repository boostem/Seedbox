/** A deck's decoded audio inside the audio thread: chunks of one source, read by file position. */

export class AudioStream {
  private readonly chunks: ({ left: Float32Array; right: Float32Array } | null)[] = [];
  /** Samples read that should have been there and weren't: the loader fell behind. */
  missing = 0;

  constructor(
    readonly frames: number,
    readonly chunkFrames: number,
  ) {}

  setChunk(index: number, left: Float32Array, right: Float32Array): void {
    this.chunks[index] = { left, right };
  }

  dropChunk(index: number): void {
    this.chunks[index] = null;
  }

  /** Samples [pos, pos + n) into left/right from `offset`; what's outside the file or not loaded is silence. */
  read(pos: number, n: number, left: Float64Array, right: Float64Array, offset: number): void {
    let at = pos;
    let o = offset;
    const end = offset + n;
    while (o < end) {
      if (at < 0 || at >= this.frames) {
        const skip = at < 0 ? Math.min(end - o, -at) : end - o;
        left.fill(0, o, o + skip);
        right.fill(0, o, o + skip);
        o += skip;
        at += skip;
        continue;
      }
      const index = Math.floor(at / this.chunkFrames);
      const within = at - index * this.chunkFrames;
      const take = Math.min(end - o, this.chunkFrames - within);
      const chunk = this.chunks[index];
      if (!chunk) {
        left.fill(0, o, o + take);
        right.fill(0, o, o + take);
        this.missing += take;
      } else {
        const { left: l, right: r } = chunk;
        const have = Math.min(take, l.length - within); // a file's last chunk may be a little short of what it said
        for (let i = 0; i < have; i++) {
          left[o + i] = l[within + i];
          right[o + i] = r[within + i];
        }
        if (have < take) {
          left.fill(0, o + Math.max(have, 0), o + take);
          right.fill(0, o + Math.max(have, 0), o + take);
        }
      }
      o += take;
      at += take;
    }
  }
}
