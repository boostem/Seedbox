// Builds mock mode and screenshots the main screens at the two reference sizes (DESIGN.md "Screenshots").
// Run: deno task screenshot [--no-build] [--out=DIR]   (default DIR: screenshots/, git-ignored)
// Chrome: $CHROME, else chrome-headless-shell on PATH, Playwright's headless shell, chromium / google-chrome on PATH
// or the macOS app bundle.

const ROOT = new URL("..", import.meta.url).pathname;
const DIST = `${ROOT}dist-mock`;
const SIZES = [[1440, 900], [1180, 760]] as const;

const args = new Set(Deno.args);
const outArg = Deno.args.find((a) => a.startsWith("--out="));
const OUT = outArg ? outArg.slice("--out=".length) : `${ROOT}screenshots`;

// The mock's richest crate: the Favour seed (src/mock/data.ts initialCrates), its folder named like sanitize().
const fixtures = JSON.parse(await Deno.readTextFile(`${ROOT}src/mock/fixtures/tunebat.json`));
const seed = fixtures.seed as { id: string; display_name: string };
const folder = encodeURIComponent(seed.display_name.replace(/[/\\:*?"<>|]/g, "_").replace(/\s+/g, " ").trim().slice(0, 180));

const ROUTES: [name: string, hash: string][] = [
  ["crates", "#/crates"],
  ["crate", `#/crate/${seed.id}`],
  ["set", `#/set/${folder}`],
  ["track", `#/set/${folder}?t=1`], // ?t=<n> opens track n in the track view
  ["mix", `#/set/${folder}?x=1`], // ?x=<n> opens transition n in the mix view
];

async function run(cmd: string, cmdArgs: string[]): Promise<void> {
  const { code } = await new Deno.Command(cmd, { args: cmdArgs, cwd: ROOT, stdout: "inherit", stderr: "inherit" }).output();
  if (code !== 0) throw new Error(`${cmd} ${cmdArgs.join(" ")} exited with ${code}`);
}

async function exists(path: string): Promise<boolean> {
  try {
    return (await Deno.stat(path)).isFile;
  } catch {
    return false;
  }
}

async function findChrome(): Promise<string> {
  const env = Deno.env.get("CHROME");
  if (env) return env;
  const dirs = (Deno.env.get("PATH") ?? "").split(":");
  const onPath = async (name: string) => {
    for (const d of dirs) if (await exists(`${d}/${name}`)) return `${d}/${name}`;
    return null;
  };
  const shell = await onPath("chrome-headless-shell");
  if (shell) return shell;
  const home = Deno.env.get("HOME") ?? "";
  for (const cache of [`${home}/.cache/ms-playwright`, `${home}/Library/Caches/ms-playwright`]) {
    try {
      for await (const e of Deno.readDir(cache)) {
        if (!e.name.startsWith("chromium_headless_shell-")) continue;
        for (const arch of ["linux64", "mac-arm64", "mac-x64"]) {
          const p = `${cache}/${e.name}/chrome-headless-shell-${arch}/chrome-headless-shell`;
          if (await exists(p)) return p;
        }
      }
    } catch {
      /* no playwright cache */
    }
  }
  for (const name of ["chromium", "chromium-browser", "google-chrome"]) {
    const p = await onPath(name);
    if (p) return p;
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (await exists(mac)) return mac;
  throw new Error("No Chrome found: set CHROME to a chrome-headless-shell or Chrome binary");
}

const TYPES: Record<string, string> = {
  html: "text/html; charset=utf-8",
  js: "text/javascript",
  css: "text/css",
  svg: "image/svg+xml",
  json: "application/json",
  png: "image/png",
  jpg: "image/jpeg",
  woff2: "font/woff2",
  mp3: "audio/mpeg",
  wav: "audio/wav",
};

/** Static server for dist-mock/ on a free port (no vite preview, so a running :4174 doesn't get in the way). */
function serve(): Deno.HttpServer<Deno.NetAddr> {
  return Deno.serve({ hostname: "127.0.0.1", port: 0, onListen: () => {} }, async (req) => {
    let path = decodeURIComponent(new URL(req.url).pathname);
    if (path.endsWith("/")) path += "index.html";
    if (path.includes("..")) return new Response("bad path", { status: 400 });
    try {
      const body = await Deno.readFile(`${DIST}${path}`);
      return new Response(body, { headers: { "content-type": TYPES[path.split(".").pop() ?? ""] ?? "application/octet-stream" } });
    } catch {
      return new Response("not found", { status: 404 });
    }
  });
}

async function shoot(chrome: string, url: string, file: string, [w, h]: readonly [number, number]): Promise<void> {
  const profile = await Deno.makeTempDir({ prefix: "seedbox-shot-" });
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 30_000);
  try {
    const { code, stderr } = await new Deno.Command(chrome, {
      args: [
        "--headless",
        "--hide-scrollbars",
        "--no-first-run",
        `--user-data-dir=${profile}`,
        `--screenshot=${file}`,
        `--window-size=${w},${h}`,
        "--virtual-time-budget=5000",
        "--autoplay-policy=no-user-gesture-required",
        url,
      ],
      stdout: "null",
      stderr: "piped",
      signal: ac.signal,
    }).output();
    if (code !== 0 || !(await exists(file))) throw new Error(`chrome failed for ${url}:\n${new TextDecoder().decode(stderr)}`);
  } finally {
    clearTimeout(timer);
    await Deno.remove(profile, { recursive: true }).catch(() => {});
  }
}

if (!args.has("--no-build")) await run(Deno.execPath(), ["task", "build:mock"]);
const chrome = await findChrome();
await Deno.mkdir(OUT, { recursive: true });
const server = serve();
try {
  const base = `http://127.0.0.1:${server.addr.port}/`;
  for (const [name, hash] of ROUTES) {
    for (const size of SIZES) {
      const file = `${OUT}/${name}-${size[0]}x${size[1]}.png`;
      await shoot(chrome, base + hash, file, size);
      console.log(file);
    }
  }
} finally {
  await server.shutdown();
}
