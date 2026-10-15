import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  // Backend address for local dev. Change via BACKEND_URL in frontend/.env if 8000 is taken.
  const target = env.BACKEND_URL || "http://localhost:8000";
  return {
    plugins: [react()],
    server: { proxy: { "/api": target } },
    preview: { proxy: { "/api": target } },
  };
});
