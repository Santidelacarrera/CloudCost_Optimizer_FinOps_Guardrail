import { expect, type APIRequestContext, type Page } from "@playwright/test";

const MAIL = process.env.MAILPIT_URL ?? "http://localhost:8025";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export const uniqueEmail = (tag: string) => `e2e-${tag}-${Date.now().toString(36)}@example.com`;

// ---------------------------------------------------------------- TOTP (RFC 6238, SHA-1, 30 s, 6 dígitos)
import { createHmac } from "node:crypto";

function base32(s: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of s.replace(/[\s=]/g, "").toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

export const currentStep = () => Math.floor(Date.now() / 30_000);

export function totp(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const h = createHmac("sha1", base32(secret)).update(counter).digest();
  const o = h[h.length - 1] & 0x0f;
  const n = ((h[o] & 0x7f) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3];
  return String(n % 1_000_000).padStart(6, "0");
}

/** Código válido para un intervalo posterior a `after`. El servidor acepta ±1 intervalo y rechaza reutilizar uno ya usado,
 *  así que si hace falta se espera a que el reloj entre en la ventana. */
export async function codeAfter(secret: string, after: number): Promise<{ code: string; step: number }> {
  const step = Math.max(after + 1, currentStep());
  while (step > currentStep() + 1) await sleep(1000);
  return { code: totp(secret, step), step };
}

// ---------------------------------------------------------------- buzón de pruebas (Mailpit)
/** Espera el correo enviado a `to` que contenga un enlace /<path>?token=… y devuelve esa ruta relativa. */
export async function linkFromMail(request: APIRequestContext, to: string, path: "verify-email" | "reset-password"): Promise<string> {
  const re = new RegExp(`https?://[^\\s"<>]+/${path}\\?token=[A-Za-z0-9_\\-]+`);
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const list = await request.get(`${MAIL}/api/v1/messages`);
    if (list.ok()) {
      const { messages = [] } = await list.json();
      for (const m of messages) {
        if (!(m.To ?? []).some((t: { Address: string }) => t.Address.toLowerCase() === to.toLowerCase())) continue;
        const full = await (await request.get(`${MAIL}/api/v1/message/${m.ID}`)).json();
        const hit = re.exec(full.Text ?? "");
        if (hit) { const u = new URL(hit[0]); return u.pathname + u.search; }
      }
    }
    await sleep(500);
  }
  throw new Error(`No llegó el correo con enlace /${path} para ${to}`);
}

// ---------------------------------------------------------------- acciones de interfaz
export async function login(page: Page, email: string, password: string) {
  await page.goto("/login");
  await page.locator("#email").fill(email);
  await page.getByLabel("Contraseña", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
}

export async function logout(page: Page) {
  await page.getByRole("button", { name: "Cerrar sesión" }).click();
  await expect(page).toHaveURL(/\/login/);
}

export const atPanel = (page: Page) => expect(page).toHaveURL(/^[^?#]*\/$/);
