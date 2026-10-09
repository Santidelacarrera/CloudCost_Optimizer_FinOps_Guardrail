import { expect, test } from "@playwright/test";

test("sin sesión, una página privada lleva al login recordando el destino", async ({ page }) => {
  await page.goto("/recommendations");
  await expect(page).toHaveURL(/\/login\?next=%2Frecommendations/);
});

test("sin sesión, la API del BFF responde 401", async ({ request }) => {
  const r = await request.get("/api/proxy/recommendations");
  expect(r.status()).toBe(401);
});

test("las cabeceras de seguridad están presentes", async ({ request }) => {
  const r = await request.get("/login");
  const h = r.headers();
  expect(h["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(h["content-security-policy"]).toContain("object-src 'none'");
  expect(h["x-frame-options"]).toBe("DENY");
  expect(h["x-content-type-options"]).toBe("nosniff");
  expect(h["referrer-policy"]).toBe("no-referrer");
  expect(h["strict-transport-security"]).toContain("max-age=");
});

const scriptSrc = (csp: string) => csp.split(";").map((d) => d.trim()).find((d) => d.startsWith("script-src ")) ?? "";

test("la CSP de scripts usa un nonce distinto por petición y no admite 'unsafe-inline'", async ({ request }) => {
  const [a, b] = await Promise.all([request.get("/login"), request.get("/login")]);
  const [cspA, cspB] = [a.headers()["content-security-policy"], b.headers()["content-security-policy"]];
  for (const csp of [cspA, cspB]) {
    expect(scriptSrc(csp)).toMatch(/'nonce-[A-Za-z0-9+/=]{20,}'/);
    expect(scriptSrc(csp)).toContain("'strict-dynamic'");
    expect(scriptSrc(csp)).not.toContain("'unsafe-inline'");
  }
  const nonce = (csp: string) => /'nonce-([^']+)'/.exec(scriptSrc(csp))![1];
  expect(nonce(cspA)).not.toBe(nonce(cspB));
  // los scripts del HTML llevan el nonce de ESA respuesta (si no, el navegador los bloquearía)
  const html = await a.text();
  const tags = html.match(/<script\b[^>]*>/g) ?? [];
  expect(tags.length).toBeGreaterThan(0);
  expect(tags.every((t) => t.includes(`nonce="${nonce(cspA)}"`))).toBe(true);
});

test("la página arranca bajo la CSP sin violaciones y el formulario responde (hidratación)", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (m) => { if (/content security policy|refused to (execute|load|apply)/i.test(m.text())) violations.push(m.text()); });
  page.on("pageerror", (e) => violations.push(e.message));
  await page.goto("/login");
  await page.locator("#email").fill("nadie@example.com");
  await page.getByLabel("Contraseña", { exact: true }).fill("una-clave-cualquiera-1");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();     // la respuesta de error solo se pinta si React hidrató y ejecutó el manejador
  expect(violations).toEqual([]);
});

test("una petición de otro origen no puede iniciar sesión (CSRF)", async ({ request }) => {
  const r = await request.post("/api/auth/login", { headers: { Origin: "https://sitio-malicioso.example" }, data: { email: "a@b.co", password: "x" } });
  expect(r.status()).toBe(403);
});

test("el acceso de desarrollo está apagado", async ({ request, baseURL }) => {
  const r = await request.post("/api/session", { headers: { Origin: baseURL! }, data: { email: "x@y.co", role: "ADMIN" } });
  expect(r.status()).toBe(404);
});
