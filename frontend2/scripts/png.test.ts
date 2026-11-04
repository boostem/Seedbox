import { describe, expect, it } from "vitest";
import { decodePng, diffImages, encodePng, type Image } from "./png";

function image(width: number, height: number, fill: (x: number, y: number) => [number, number, number, number]): Image {
  const data = new Uint8Array(width * height * 4);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) data.set(fill(x, y), (y * width + x) * 4);
  return { width, height, data };
}

describe("png", () => {
  it("round-trips RGBA", async () => {
    const img = image(7, 5, (x, y) => [x * 30, y * 50, (x * y) % 256, 255 - x]);
    const back = await decodePng(await encodePng(img));
    expect(back.width).toBe(7);
    expect(back.height).toBe(5);
    expect([...back.data]).toEqual([...img.data]);
  });

  it("rejects what isn't a PNG", async () => {
    await expect(decodePng(new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]))).rejects.toThrow("not a PNG");
  });

  it("counts pixels that moved more than the threshold", () => {
    const a = image(10, 10, () => [100, 100, 100, 255]);
    const b = image(10, 10, (x, y) => (x === 0 && y < 3 ? [200, 100, 100, 255] : x === 1 ? [110, 100, 100, 255] : [100, 100, 100, 255]));
    const d = diffImages(a, b, 24);
    expect(d.changed).toBe(3); // column 1 moved by 10: under the threshold
    expect(d.total).toBe(100);
    expect([...d.image.data.subarray(0, 4)]).toEqual([255, 0, 64, 255]);
  });

  it("fails on a size change", () => {
    expect(() => diffImages(image(2, 2, () => [0, 0, 0, 255]), image(3, 2, () => [0, 0, 0, 255]))).toThrow("size changed");
  });
});
