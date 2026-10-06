import { NextRequest, NextResponse } from "next/server";
import { PENDING_COOKIE, SESSION_COOKIE, callApi, clearCookie, devClaims, json, relay, sameOrigin, setCookie } from "@/lib/server/bff";

// Flujos de autenticación. Los pasos que emiten tokens (login, 2FA) viven aquí para guardar el token en una cookie httpOnly y no
// devolverlo nunca al JavaScript del navegador. El resto de acciones de cuenta (sesiones, 2FA, equipo) pasan por /api/proxy.
const PUBLIC_POST = new Set(["signup", "verify-email", "resend-verification", "forgot-password", "reset-password"]);

async function handler(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const key = (await ctx.params).path.join("/");

  if (req.method === "GET") {
    if (key === "config") return relay(await callApi("auth/config"));
    if (key === "me") return me(req);
    return json({ detail: "No encontrado" }, 404);
  }
  if (req.method !== "POST") return json({ detail: "Método no permitido" }, 405);
  if (!sameOrigin(req)) return json({ detail: "Origen no permitido" }, 403);

  if (PUBLIC_POST.has(key)) return relay(await callApi(`auth/${key}`, { method: "POST", body: await req.text(), req }));
  if (key === "login") return login(req);
  if (key === "mfa/verify") return mfaVerify(req);
  if (key === "logout") return logout(req);
  return json({ detail: "No encontrado" }, 404);
}

async function login(req: NextRequest) {
  const r = await callApi("auth/login", { method: "POST", body: await req.text(), req });
  if (!r.ok) return relay(r);
  const data = await r.json();
  const res = json({ status: data.status });
  if (data.status === "ok") {
    setCookie(res, SESSION_COOKIE, data.token, data.expires_in);
    clearCookie(res, PENDING_COOKIE);
  } else if (data.status === "mfa_required") {
    setCookie(res, PENDING_COOKIE, data.pending_token, data.expires_in);
  }
  return res;
}

async function mfaVerify(req: NextRequest) {
  const pending = req.cookies.get(PENDING_COOKIE)?.value;
  const body = await req.json().catch(() => null);
  if (!pending || typeof body?.code !== "string") {
    const res = json({ detail: "La verificación venció. Inicia sesión de nuevo.", code: "mfa_expired" }, 401);
    clearCookie(res, PENDING_COOKIE);
    return res;
  }
  const r = await callApi("auth/mfa/verify", { method: "POST", body: JSON.stringify({ pending_token: pending, code: body.code }), req });
  const text = await r.text();
  let data: { code?: string; token?: string; expires_in?: number; recovery_codes_left?: number } = {};
  try { data = JSON.parse(text); } catch { /* respuesta no JSON */ }
  if (!r.ok) {
    const retry = r.headers.get("retry-after");
    const res = new NextResponse(text, { status: r.status, headers: { "Cache-Control": "no-store", "Content-Type": "application/json", ...(retry ? { "Retry-After": retry } : {}) } });
    if (data.code === "mfa_expired" || data.code === "locked") clearCookie(res, PENDING_COOKIE);   // un código incorrecto conserva el intento
    return res;
  }
  const res = json({ status: "ok", recovery_codes_left: data.recovery_codes_left ?? null });
  setCookie(res, SESSION_COOKIE, data.token ?? "", data.expires_in ?? 0);
  clearCookie(res, PENDING_COOKIE);
  return res;
}

async function logout(req: NextRequest) {
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  if (token?.startsWith("ccs_")) await callApi("auth/logout", { method: "POST", token, req }).catch(() => null);   // revoca en el servidor
  const res = json({ ok: true });
  clearCookie(res, SESSION_COOKIE);
  clearCookie(res, PENDING_COOKIE);
  return res;
}

async function me(req: NextRequest) {
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  if (!token) return json({ detail: "No autenticado" }, 401);
  if (token.startsWith("ccs_")) return relay(await callApi("auth/me", { token, req }));
  const c = devClaims(token);          // sesión de desarrollo (AUTH_MODE=dev): no hay cuenta real detrás
  return json({ mode: "dev", email: c?.email ?? "demo@example.com", full_name: "Sesión de desarrollo", role: c?.role ?? "VIEWER",
                organization: "Organización demo", mfa_enabled: false, mfa_recommended: false });
}

export { handler as GET, handler as POST };
