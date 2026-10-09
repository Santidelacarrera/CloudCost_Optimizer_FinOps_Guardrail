import { expect, test } from "@playwright/test";
import { linkFromMail, login, uniqueEmail } from "./helpers";

// Organización nueva (sin recursos ni costos): el gráfico de ahorro proyectado debe pintarse igual, con ceros, y exponer sus datos.
const EMAIL = uniqueEmail("grafico");
const PASSWORD = "Tortuga-Azul-Lluvia-4821";

test("el panel muestra el gráfico «gasto actual frente a optimizado» con su tabla de datos", async ({ page, request }) => {
  await page.goto("/signup");
  await page.getByLabel("Tu nombre").fill("Ana Pérez");
  await page.getByLabel("Nombre de la organización").fill("Org Gráfico SpA");
  await page.getByLabel("Correo").fill(EMAIL);
  await page.getByLabel("Contraseña", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Crear cuenta" }).click();
  await expect(page.getByRole("heading", { name: "Revisa tu correo" })).toBeVisible();

  await page.goto(await linkFromMail(request, EMAIL, "verify-email"));
  await page.getByRole("button", { name: "Confirmar mi correo" }).click();

  await login(page, EMAIL, PASSWORD);
  await expect(page).toHaveURL(/^[^?#]*\/$/);
  const chart = page.getByTestId("savings-chart");
  await expect(chart).toBeVisible();
  await expect(chart.getByRole("img")).toHaveAttribute("aria-label", /Gasto mensual actual/);
  await chart.getByText("Ver los datos y los supuestos").click();
  await expect(chart.getByRole("row")).toHaveCount(1 + 3 + 1 + 6);       // cabecera + 3 meses de historial + mes en curso + 6 de proyección
});
