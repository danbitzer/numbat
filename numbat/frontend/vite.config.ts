import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // Served behind HA ingress at an unpredictable path prefix — every asset
  // URL must be relative.
  base: "./",
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  plugins: [
    react({
      babel: { plugins: [["babel-plugin-react-compiler", {}]] },
    }),
    tailwindcss(),
  ],
  build: {
    // Straight into the Python package: the Dockerfile's `COPY src ./src`
    // ships it, and the FastAPI app serves it. CI builds this before the
    // add-on image; locally run `bun run build` (dir is gitignored).
    outDir: "../src/numbat/web/dist",
    emptyOutDir: true,
  },
  server: {
    // Dev HMR against a running Numbat (add-on dev loop) on :8099.
    proxy: {
      "/api": "http://localhost:8099",
      "/health": "http://localhost:8099",
    },
  },
});
