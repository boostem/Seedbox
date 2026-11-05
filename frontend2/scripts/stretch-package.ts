/** What's vendored from the npm package, for `vendor-stretch.ts` and the test that holds the copy to it. */
export const STRETCH_VERSION = "1.3.2";

/** The WebAssembly in the package's module, as base64. */
export function wasmOfPackage(source: string): string {
  const found = /application\/octet-stream;base64,([A-Za-z0-9+/=]+)/.exec(source);
  if (!found) throw new Error("no WebAssembly in the package");
  return found[1];
}
