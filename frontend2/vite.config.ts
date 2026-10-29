import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Seedbox 2's dev ports, so it runs next to Seedbox 1 (frontend/ on 5173 → 8765)
const BACKEND = "http://127.0.0.1:8766";

export default defineConfig({
  base: "./", // relative asset URLs: dist/ can be served from any origin/path (pywebview, FastAPI)
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 5174,
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
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 900 },
});
