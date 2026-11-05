import "node:module";
import { fileURLToPath } from "node:url";
import tailwindcss from "file:///home/boostem/.local/share/ultrakanban-agent/boards/w118DWMEAfM/worktrees/2/frontend2/node_modules/.deno/@tailwindcss+vite@4.3.3/node_modules/@tailwindcss/vite/dist/index.mjs";
import react from "file:///home/boostem/.local/share/ultrakanban-agent/boards/w118DWMEAfM/worktrees/2/frontend2/node_modules/.deno/@vitejs+plugin-react@6.1.1/node_modules/@vitejs/plugin-react/dist/index.js";
import { defineConfig } from "file:///home/boostem/.local/share/ultrakanban-agent/boards/w118DWMEAfM/worktrees/2/frontend2/node_modules/.deno/vite@8.3.2/node_modules/vite/dist/node/index.js";
var vite_config_default = defineConfig({
	base: "./",
	plugins: [react(), tailwindcss()],
	resolve: { alias: { "@": fileURLToPath(new URL("./src", "file:///home/boostem/.local/share/ultrakanban-agent/boards/w118DWMEAfM/worktrees/2/frontend2/vite.config.ts")) } },
	server: {
		port: 5174,
		strictPort: true,
		proxy: { "/api": {
			target: "http://127.0.0.1:8766",
			changeOrigin: true,
			configure: (proxy) => {
				proxy.on("proxyReq", (req) => req.setHeader("accept-encoding", "identity"));
			}
		} }
	},
	build: {
		outDir: "dist",
		emptyOutDir: true,
		sourcemap: false,
		chunkSizeWarningLimit: 900
	}
});
//#endregion
export { vite_config_default as default };

//# sourceMappingURL=data:application/json;charset=utf-8;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoidml0ZS5jb25maWcuanMiLCJuYW1lcyI6W10sInNvdXJjZXMiOlsiL2hvbWUvYm9vc3RlbS8ubG9jYWwvc2hhcmUvdWx0cmFrYW5iYW4tYWdlbnQvYm9hcmRzL3cxMThEV01FQWZNL3dvcmt0cmVlcy8yL2Zyb250ZW5kMi92aXRlLmNvbmZpZy50cyJdLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgeyBmaWxlVVJMVG9QYXRoIH0gZnJvbSBcIm5vZGU6dXJsXCI7XG5pbXBvcnQgdGFpbHdpbmRjc3MgZnJvbSBcIkB0YWlsd2luZGNzcy92aXRlXCI7XG5pbXBvcnQgcmVhY3QgZnJvbSBcIkB2aXRlanMvcGx1Z2luLXJlYWN0XCI7XG5pbXBvcnQgeyBkZWZpbmVDb25maWcgfSBmcm9tIFwidml0ZVwiO1xuXG4vLyBTZWVkYm94IDIncyBkZXYgcG9ydHMsIHNvIGl0IHJ1bnMgbmV4dCB0byBTZWVkYm94IDEgKGZyb250ZW5kLyBvbiA1MTczIOKGkiA4NzY1KVxuY29uc3QgQkFDS0VORCA9IFwiaHR0cDovLzEyNy4wLjAuMTo4NzY2XCI7XG5cbmV4cG9ydCBkZWZhdWx0IGRlZmluZUNvbmZpZyh7XG4gIGJhc2U6IFwiLi9cIiwgLy8gcmVsYXRpdmUgYXNzZXQgVVJMczogZGlzdC8gY2FuIGJlIHNlcnZlZCBmcm9tIGFueSBvcmlnaW4vcGF0aCAocHl3ZWJ2aWV3LCBGYXN0QVBJKVxuICBwbHVnaW5zOiBbcmVhY3QoKSwgdGFpbHdpbmRjc3MoKV0sXG4gIHJlc29sdmU6IHsgYWxpYXM6IHsgXCJAXCI6IGZpbGVVUkxUb1BhdGgobmV3IFVSTChcIi4vc3JjXCIsIGltcG9ydC5tZXRhLnVybCkpIH0gfSxcbiAgc2VydmVyOiB7XG4gICAgcG9ydDogNTE3NCxcbiAgICBzdHJpY3RQb3J0OiB0cnVlLFxuICAgIHByb3h5OiB7XG4gICAgICBcIi9hcGlcIjoge1xuICAgICAgICB0YXJnZXQ6IEJBQ0tFTkQsXG4gICAgICAgIGNoYW5nZU9yaWdpbjogdHJ1ZSxcbiAgICAgICAgLy8gU1NFOiBrZWVwIHRoZSBzdHJlYW0gdW5idWZmZXJlZC91bmNvbXByZXNzZWQgdGhyb3VnaCB0aGUgcHJveHlcbiAgICAgICAgY29uZmlndXJlOiAocHJveHkpID0+IHtcbiAgICAgICAgICBwcm94eS5vbihcInByb3h5UmVxXCIsIChyZXEpID0+IHJlcS5zZXRIZWFkZXIoXCJhY2NlcHQtZW5jb2RpbmdcIiwgXCJpZGVudGl0eVwiKSk7XG4gICAgICAgIH0sXG4gICAgICB9LFxuICAgIH0sXG4gIH0sXG4gIGJ1aWxkOiB7IG91dERpcjogXCJkaXN0XCIsIGVtcHR5T3V0RGlyOiB0cnVlLCBzb3VyY2VtYXA6IGZhbHNlLCBjaHVua1NpemVXYXJuaW5nTGltaXQ6IDkwMCB9LFxufSk7XG4iXSwibWFwcGluZ3MiOiI7Ozs7O0FBUUEsSUFBQSxzQkFBZSxhQUFhO0NBQzFCLE1BQU07Q0FDTixTQUFTLENBQUMsTUFBTSxHQUFHLFlBQVksQ0FBQztDQUNoQyxTQUFTLEVBQUUsT0FBTyxFQUFFLEtBQUssY0FBYyxJQUFJLElBQUksU0FBQSw2R0FBd0IsQ0FBQyxFQUFFLEVBQUU7Q0FDNUUsUUFBUTtFQUNOLE1BQU07RUFDTixZQUFZO0VBQ1osT0FBTyxFQUNMLFFBQVE7R0FDTixRQUFRO0dBQ1IsY0FBYztHQUVkLFlBQVksVUFBVTtJQUNwQixNQUFNLEdBQUcsYUFBYSxRQUFRLElBQUksVUFBVSxtQkFBbUIsVUFBVSxDQUFDO0dBQzVFO0VBQ0YsRUFDRjtDQUNGO0NBQ0EsT0FBTztFQUFFLFFBQVE7RUFBUSxhQUFhO0VBQU0sV0FBVztFQUFPLHVCQUF1QjtDQUFJO0FBQzNGLENBQUMifQ==