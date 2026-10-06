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

test("una petición de otro origen no puede iniciar sesión (CSRF)", async ({ request }) => {
  const r = await request.post("/api/auth/login", { headers: { Origin: "https://sitio-malicioso.example" }, data: { email: "a@b.co", password: "x" } });
  expect(r.status()).toBe(403);
});

test("el acceso de desarrollo está apagado", async ({ request, baseURL }) => {
  const r = await request.post("/api/session", { headers: { Origin: baseURL! }, data: { email: "x@y.co", role: "ADMIN" } });
  expect(r.status()).toBe(404);
});
