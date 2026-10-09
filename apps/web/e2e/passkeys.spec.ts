import { expect, test } from "@playwright/test";
import { atPanel, linkFromMail, login, logout, uniqueEmail } from "./helpers";

// Llaves de acceso de punta a punta con un autenticador VIRTUAL de Chromium (CDP): registro, segundo factor, entrada sin contraseña y retirada.
// Todo en un solo test porque la cuenta y el autenticador virtual viven en el contexto del navegador.
const EMAIL = uniqueEmail("passkey");
const PASSWORD = "Tortuga-Azul-Lluvia-4821";

test("llaves de acceso: registrar, usar como 2FA, entrar sin contraseña y quitar", async ({ page, context, request }) => {
  const cdp = await context.newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true },
  });

  await test.step("cuenta nueva confirmada y con sesión", async () => {
    await page.goto("/signup");
    await page.getByLabel("Tu nombre").fill("Ana Pérez");
    await page.getByLabel("Nombre de la organización").fill("Constructora Llaves SpA");
    await page.getByLabel("Correo").fill(EMAIL);
    await page.getByLabel("Contraseña", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Crear cuenta" }).click();
    await expect(page.getByRole("heading", { name: "Revisa tu correo" })).toBeVisible();
    await page.goto(await linkFromMail(request, EMAIL, "verify-email"));
    await page.getByRole("button", { name: "Confirmar mi correo" }).click();
    await expect(page.getByRole("heading", { name: "Correo confirmado" })).toBeVisible();
    await login(page, EMAIL, PASSWORD);
    await atPanel(page);
  });

  await test.step("registrar una llave de acceso (pide la contraseña)", async () => {
    await page.goto("/account");
    await page.getByRole("button", { name: "Añadir una llave de acceso" }).click();
    await page.getByLabel("Tu contraseña").fill(PASSWORD);
    await page.getByLabel("Nombre (opcional)").fill("Llave virtual");
    await page.getByRole("button", { name: "Continuar" }).click();
    await expect(page.getByText("Llave de acceso añadida.")).toBeVisible();
    await expect(page.getByText("Llave virtual")).toBeVisible();
  });

  await test.step("con la llave registrada, la contraseña ya no basta: se pide la llave", async () => {
    await logout(page);
    await login(page, EMAIL, PASSWORD);
    await expect(page).toHaveURL(/\/mfa/);
    await page.getByRole("button", { name: "Usar llave de acceso" }).click();
    await atPanel(page);
  });

  await test.step("entrar sin contraseña con la llave", async () => {
    await logout(page);
    await page.getByRole("button", { name: "Entrar con llave de acceso" }).click();
    await atPanel(page);
    await expect(page.getByRole("button", { name: "Cerrar sesión" })).toBeVisible();
    const cookie = (await context.cookies()).find((c) => c.name === "cc_session");
    expect(cookie?.httpOnly).toBe(true);
  });

  await test.step("quitar la llave (pide la contraseña) y volver a entrar solo con contraseña", async () => {
    await page.goto("/account");
    await page.getByRole("button", { name: "Quitar", exact: true }).click();
    await page.getByLabel("Tu contraseña").fill(PASSWORD);
    await page.locator("form.step").getByRole("button", { name: "Quitar", exact: true }).click();
    await expect(page.getByText("Llave de acceso quitada")).toBeVisible();
    await logout(page);
    await login(page, EMAIL, PASSWORD);
    await atPanel(page);
  });
});
