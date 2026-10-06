import { expect, test } from "@playwright/test";
import { atPanel, codeAfter, linkFromMail, login, logout, uniqueEmail } from "./helpers";

// Un solo recorrido encadenado: la cuenta nace, se confirma, activa 2FA, se recupera y se vuelve a entrar. Cada paso depende del anterior.
test.describe.configure({ mode: "serial" });

const NAME = "Ana Pérez";
const ORG = "Constructora Prueba SpA";
const EMAIL = uniqueEmail("viaje");
const PASSWORD = "Tortuga-Azul-Lluvia-4821";
const NEW_PASSWORD = "Cometa-Verde-Nube-7352!";

let secret = "";
let lastStep = 0;
let recovery: string[] = [];

test("registro: la contraseña débil no se puede enviar", async ({ page }) => {
  await page.goto("/signup");
  await page.getByLabel("Tu nombre").fill(NAME);
  await page.getByLabel("Nombre de la organización").fill(ORG);
  await page.getByLabel("Correo").fill(EMAIL);
  await page.getByLabel("Contraseña", { exact: true }).fill("password123");
  await expect(page.getByRole("button", { name: "Crear cuenta" })).toBeDisabled();
});

test("registro: crea la cuenta y pide revisar el correo", async ({ page }) => {
  await page.goto("/signup");
  await page.getByLabel("Tu nombre").fill(NAME);
  await page.getByLabel("Nombre de la organización").fill(ORG);
  await page.getByLabel("Correo").fill(EMAIL);
  await page.getByLabel("Contraseña", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Crear cuenta" }).click();
  await expect(page.getByRole("heading", { name: "Revisa tu correo" })).toBeVisible();
});

test("login: sin confirmar el correo no entra", async ({ page }) => {
  await login(page, EMAIL, PASSWORD);
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/login/);
});

test("correo: el enlace confirma la cuenta (solo al pulsar el botón)", async ({ page, request }) => {
  const link = await linkFromMail(request, EMAIL, "verify-email");
  await page.goto(link);
  await expect(page.getByRole("heading", { name: "Confirma tu correo" })).toBeVisible();   // abrir el enlace no consume el token
  await page.getByRole("button", { name: "Confirmar mi correo" }).click();
  await expect(page.getByRole("heading", { name: "Correo confirmado" })).toBeVisible();
});

test("login: entra, la cookie es httpOnly y SameSite=Strict", async ({ page, context }) => {
  await login(page, EMAIL, PASSWORD);
  await atPanel(page);
  await expect(page.getByRole("button", { name: "Cerrar sesión" })).toBeVisible();
  await expect(page.getByText(NAME).first()).toBeVisible();

  const cookie = (await context.cookies()).find((c) => c.name === "cc_session");
  expect(cookie, "debe existir la cookie de sesión").toBeTruthy();
  expect(cookie!.httpOnly).toBe(true);
  expect(cookie!.sameSite).toBe("Strict");
  expect(await page.evaluate(() => document.cookie)).not.toContain("cc_session");   // el JavaScript de la página no la ve
});

test("login: contraseña errónea y correo inexistente responden igual (sin enumerar usuarios)", async ({ request, baseURL }) => {
  const headers = { Origin: baseURL! };
  const wrong = await request.post("/api/auth/login", { headers, data: { email: EMAIL, password: "Otra-Clave-Incorrecta-1!" } });
  const unknown = await request.post("/api/auth/login", { headers, data: { email: uniqueEmail("nadie"), password: "Otra-Clave-Incorrecta-1!" } });
  expect(wrong.status()).toBe(401);
  expect(unknown.status()).toBe(wrong.status());
  expect(await unknown.json()).toEqual(await wrong.json());
});

test("2FA: activa con una app de autenticación y entrega 10 códigos de recuperación", async ({ page }) => {
  await login(page, EMAIL, PASSWORD);
  await atPanel(page);
  await page.goto("/account");
  await page.getByRole("button", { name: "Activar verificación en dos pasos" }).click();
  await page.getByLabel("Tu contraseña").fill(PASSWORD);
  await page.getByRole("button", { name: "Continuar" }).click();

  await expect(page.locator(".secret")).toBeVisible();
  secret = (await page.locator(".secret").innerText()).replace(/\s/g, "");
  expect(secret).toMatch(/^[A-Z2-7]{32}$/);

  const { code, step } = await codeAfter(secret, 0);
  lastStep = step;
  await page.locator("#otp").fill(code);
  await page.getByRole("button", { name: "Activar", exact: true }).click();

  await expect(page.getByText("Guarda tus códigos de recuperación")).toBeVisible();
  recovery = await page.locator(".codes [role=listitem]").allInnerTexts();
  expect(recovery).toHaveLength(10);
  await page.getByLabel("Ya los guardé en un lugar seguro").check();
  await page.getByRole("button", { name: "Listo" }).click();
  await expect(page.getByText(/Te quedan/)).toBeVisible();
});

test("2FA: el login pide el código y no acepta reutilizar uno ya usado", async ({ page }) => {
  // Cada prueba tiene su propio navegador limpio: aquí no hay sesión previa.
  await login(page, EMAIL, PASSWORD);
  await expect(page).toHaveURL(/\/mfa/);

  const { code, step } = await codeAfter(secret, lastStep);
  lastStep = step;
  await page.locator("#code").fill(code);                       // se envía solo al completar 6 dígitos
  await atPanel(page);

  // Mismo código otra vez: la API lo rechaza aunque siga dentro de la ventana de tiempo.
  await logout(page);
  await login(page, EMAIL, PASSWORD);
  await expect(page).toHaveURL(/\/mfa/);
  await page.locator("#code").fill(code);
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/mfa/);
});

test("2FA: un código de recuperación sirve una sola vez", async ({ page }) => {
  await login(page, EMAIL, PASSWORD);
  await expect(page).toHaveURL(/\/mfa/);
  await page.getByRole("button", { name: "Usar un código de recuperación" }).click();
  await page.locator("#code").fill(recovery[0]);
  await page.getByRole("button", { name: "Verificar" }).click();
  await atPanel(page);

  await logout(page);
  await login(page, EMAIL, PASSWORD);
  await page.getByRole("button", { name: "Usar un código de recuperación" }).click();
  await page.locator("#code").fill(recovery[0]);                // el mismo código ya está gastado
  await page.getByRole("button", { name: "Verificar" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/mfa/);
});

test("recuperación: restablece la contraseña por correo y la antigua deja de servir", async ({ page, request }) => {
  await page.goto("/forgot-password");
  await page.locator("#email").fill(EMAIL);
  await page.getByRole("button", { name: "Enviar enlace" }).click();
  await expect(page.getByRole("heading", { name: "Revisa tu correo" })).toBeVisible();

  const link = await linkFromMail(request, EMAIL, "reset-password");
  await page.goto(link);
  await page.getByLabel("Contraseña nueva", { exact: true }).fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Guardar contraseña" }).click();
  await expect(page).toHaveURL(/\/login\?reset=1/);
  await expect(page.getByText("Contraseña actualizada")).toBeVisible();

  // El enlace es de un solo uso.
  await page.goto(link);
  await page.getByLabel("Contraseña nueva", { exact: true }).fill("Otra-Clave-Distinta-9911!");
  await page.getByRole("button", { name: "Guardar contraseña" }).click();
  await expect(page.getByRole("heading", { name: "Este enlace ya no sirve" })).toBeVisible();

  await login(page, EMAIL, PASSWORD);                            // la contraseña vieja
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page).toHaveURL(/\/login/);

  await login(page, EMAIL, NEW_PASSWORD);                        // la nueva, y el 2FA se sigue pidiendo
  await expect(page).toHaveURL(/\/mfa/);
  await page.getByRole("button", { name: "Usar un código de recuperación" }).click();
  await page.locator("#code").fill(recovery[1]);
  await page.getByRole("button", { name: "Verificar" }).click();
  await atPanel(page);
});

test("sesiones: la pantalla de cuenta lista esta sesión", async ({ page }) => {
  await login(page, EMAIL, NEW_PASSWORD);
  await expect(page).toHaveURL(/\/mfa/);
  await page.getByRole("button", { name: "Usar un código de recuperación" }).click();
  await page.locator("#code").fill(recovery[2]);
  await page.getByRole("button", { name: "Verificar" }).click();
  await atPanel(page);
  await page.goto("/account");
  await expect(page.getByRole("heading", { name: "Sesiones activas" })).toBeVisible();
  await expect(page.getByText("Esta sesión")).toBeVisible();
});
