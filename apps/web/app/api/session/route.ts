import { NextRequest } from "next/server";
import { SESSION_COOKIE, callApi, clearCookie, json, sameOrigin, setCookie } from "@/lib/server/bff";

// Seguro por omisión: solo se habilita si DEV_LOGIN=dev está declarado de forma explícita (docker-compose lo deriva de AUTH_MODE).
const DEV_LOGIN = process.env.DEV_LOGIN === "dev";

// SOLO DESARROLLO (AUTH_MODE=dev): pide un token de demostración y lo guarda en la misma cookie httpOnly que las cuentas reales.
// Con cuentas propias u OIDC esta ruta responde 404.
export async function POST(req: NextRequest) {
  if (!DEV_LOGIN) return json({ detail: "Login de desarrollo deshabilitado" }, 404);
  if (!sameOrigin(req)) return json({ detail: "Origen no permitido" }, 403);
  const body = await req.json().catch(() => null);
  if (!body?.email || !body?.role) return json({ detail: "email y role requeridos" }, 400);
  const r = await callApi("dev/token", { method: "POST", body: JSON.stringify({ email: body.email, role: body.role }) });
  if (!r.ok) return json({ detail: "No se pudo iniciar sesión" }, r.status);
  const { access_token } = await r.json();
  const res = json({ ok: true });
  setCookie(res, SESSION_COOKIE, access_token, 8 * 3600);
  return res;
}

export async function DELETE(req: NextRequest) {
  if (!sameOrigin(req)) return json({ detail: "Origen no permitido" }, 403);
  const res = json({ ok: true });
  clearCookie(res, SESSION_COOKIE);
  return res;
}
