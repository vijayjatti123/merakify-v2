import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { randomUUID } from "node:crypto";

// The manifest and the bundle carry the SAME ID, minted once per build.
const version = `${new Date().toISOString()}-${randomUUID()}`;
const manifest = JSON.stringify({ version });
const buildVersion = {
  name: "build-version",
  generateBundle() { this.emitFile({ type: "asset", fileName: "build-version.json", source: manifest }); },
  configureServer(server) {
    server.middlewares.use("/build-version.json", (_req, res) => {
      res.setHeader("Content-Type", "application/json");
      res.setHeader("Cache-Control", "no-store");
      res.end(manifest);
    });
  },
};

export default defineConfig({
  plugins: [react(), buildVersion],
  define: { __BUILD_VERSION__: JSON.stringify(version) },
  server: { port: 5173 },
});
