/** A mono 16-bit WAV of a quiet 110 Hz tone: real playback without a backend or an audio file. */
export function toneWav(seconds: number, sr = 8000): Blob {
  const n = Math.ceil(seconds * sr);
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (at: number, s: string) => [...s].forEach((c, i) => v.setUint8(at + i, c.charCodeAt(0)));
  str(0, "RIFF");
  v.setUint32(4, 36 + n * 2, true);
  str(8, "WAVEfmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, sr, true);
  v.setUint32(28, sr * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, "data");
  v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.round(800 * Math.sin((2 * Math.PI * 110 * i) / sr)), true);
  return new Blob([buf], { type: "audio/wav" });
}
