import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const OUT = path.resolve(__dirname, "../../../docs/images");
const shot = (page: Page, name: string, fullPage = false) =>
  page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage });

async function demoLogin(page: Page) {
  await page.goto("/login");
  const box = page.locator("details.dev-box");
  await expect(box, "Esta captura necesita AUTH_MODE=dev (modo demostración) en .env").toBeVisible();
  if (!(await box.evaluate((el) => (el as HTMLDetailsElement).open))) await box.locator("summary").click();
  await page.getByLabel("Rol").selectOption("ADMIN");
  await page.getByRole("button", { name: "Entrar con datos de ejemplo" }).click();
  await expect(page.getByRole("heading", { name: "Panel" })).toBeVisible();
}

test("capturas del README", async ({ page }) => {
  // 1) Pantallas públicas
  await page.goto("/login");
  await page.waitForLoadState("networkidle");
  await shot(page, "01-login");
  await page.goto("/signup");
  await page.waitForLoadState("networkidle");
  await shot(page, "02-signup");

  // 2) Entrar y ejecutar un escaneo
  await demoLogin(page);
  await page.getByRole("button", { name: "Ejecutar escaneo" }).click();
  // El escaneo lo procesa el worker: se espera a que aparezcan recomendaciones.
  await page.goto("/recommendations");
  await expect.poll(async () => {
    await page.reload({ waitUntil: "networkidle" });
    return page.locator("tbody tr a[href^='/recommendations/']").count();
  }, { timeout: 90_000, intervals: [3_000] }).toBeGreaterThan(0);

  await page.goto("/");
  await page.waitForLoadState("networkidle");
  await shot(page, "03-panel");

  await page.goto("/recommendations");
  await page.waitForLoadState("networkidle");
  await shot(page, "04-recomendaciones");

  await page.locator("tbody tr a[href^='/recommendations/']").first().click();
  await page.waitForURL(/\/recommendations\/.+/);
  await page.waitForLoadState("networkidle");
  await shot(page, "05-detalle-recomendacion", true);

  // 3) Resto de secciones
  for (const [route, name] of [
    ["/import", "06-importar-csv"],
    ["/expenses", "07-analizar-gastos"],
    ["/audit", "08-auditoria"],
    ["/team", "09-equipo"],
    ["/account", "10-cuenta-y-seguridad"],
  ] as const) {
    await page.goto(route);
    await page.waitForLoadState("networkidle");
    await shot(page, name);
  }
});
