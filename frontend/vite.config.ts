import process from "node:process";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Two apps, one frontend: index.html = Seedbox, setbox.html = Setbox (both pages are built). Each has its own dev
// server: `deno task dev` (Seedbox, :5173 -> API :8765) and `deno task dev:setbox` (Setbox, :5174 -> API :8766).
const SETBOX = !!process.env.SETBOX_DEV;
const BACKEND = `http://127.0.0.1:${SETBOX ? 8766 : 8765}`;
const page = (name: string) => fileURLToPath(new URL(name, import.meta.url));

export default defineConfig({
  base: "./", // relative asset URLs: dist/ can be served from any origin/path (pywebview, FastAPI)
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: SETBOX ? 5174 : 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        // SSE: keep the stream unbuffered/uncompressed through the proxy
        configure: (proxy) => {
          proxy.on("proxyReq", (req) => req.setHeader("accept-encoding", "identity"));
        },
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: { input: { index: page("./index.html"), setbox: page("./setbox.html") } },
  },
});
