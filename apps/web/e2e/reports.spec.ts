import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { atPanel, linkFromMail, login, uniqueEmail } from "./helpers";

const EMAIL = uniqueEmail("informe");
const PASSWORD = "Tortuga-Azul-Lluvia-4821";

test("el panel descarga el informe ejecutivo en PDF y Excel sin corromper los bytes", async ({ page, request }) => {
  await page.goto("/signup");
  await page.getByLabel("Tu nombre").fill("Ana Pérez");
  await page.getByLabel("Nombre de la organización").fill("Informe Prueba SpA");
  await page.getByLabel("Correo").fill(EMAIL);
  await page.getByLabel("Contraseña", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "Crear cuenta" }).click();
  await expect(page.getByRole("heading", { name: "Revisa tu correo" })).toBeVisible();
  await page.goto(await linkFromMail(request, EMAIL, "verify-email"));
  await page.getByRole("button", { name: "Confirmar mi correo" }).click();
  await expect(page.getByRole("heading", { name: "Correo confirmado" })).toBeVisible();
  await login(page, EMAIL, PASSWORD);
  await atPanel(page);

  const [pdf] = await Promise.all([page.waitForEvent("download"), page.getByTestId("export-pdf").click()]);
  expect(pdf.suggestedFilename()).toMatch(/^informe-ejecutivo-\d{4}-\d{2}-\d{2}\.pdf$/);
  const pdfBytes = readFileSync(await pdf.path());
  expect(pdfBytes.subarray(0, 5).toString("latin1")).toBe("%PDF-");           // un relé que lea el cuerpo como texto lo rompería
  expect(pdfBytes.subarray(-6).toString("latin1")).toContain("%%EOF");

  const [xlsx] = await Promise.all([page.waitForEvent("download"), page.getByTestId("export-xlsx").click()]);
  expect(xlsx.suggestedFilename()).toMatch(/\.xlsx$/);
  expect(readFileSync(await xlsx.path()).subarray(0, 2).toString("latin1")).toBe("PK");   // .xlsx es un ZIP
});
