import { defineConfig, devices } from "@playwright/test";

// Capturas para el README. Requiere la demo en marcha (docker compose up --build) con AUTH_MODE=dev.
//   cd apps/web && npm install && npx playwright install chromium
//   npx playwright test -c playwright.screenshots.config.ts
// Las imágenes quedan en docs/images/.
export default defineConfig({
  testDir: "./e2e-screenshots",
  timeout: 180_000,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5985",
    locale: "es-CL",
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 } }],
});
