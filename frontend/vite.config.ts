import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const BACKEND = "http://127.0.0.1:8765";

export default defineConfig({
  base: "./", // relative asset URLs: dist/ can be served from any origin/path (pywebview, FastAPI)
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5173,
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
  // mock mode's chunk carries the engine fixtures (src/mock/fixtures/engine.json, ~3 MB of JSON)
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: process.env.VITE_MOCK ? 4000 : 900 },
});
