import { defineConfig, devices } from "@playwright/test";

// Pruebas de punta a punta contra el stack real (API + Postgres + web + buzón de pruebas Mailpit).
// Local:  docker compose up -d --build && E2E_BASE_URL=http://localhost:5985 MAILPIT_URL=http://localhost:8025 npx playwright test
// CI:     job "e2e" de .github/workflows/ci.yml
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,                                    // el recorrido de cuenta es una secuencia y la API limita registros por IP
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    locale: "es-CL",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
