// Utilidades del BFF (solo servidor). El navegador nunca ve el token de sesión: vive en una cookie httpOnly.
import { NextRequest, NextResponse } from "next/server";

export const API = process.env.API_URL ?? "http://localhost:8000";

// Con HTTPS se usa el prefijo __Host- (el navegador exige Secure, Path=/ y ningún Domain: la cookie no puede fijarse desde un subdominio).
export const SECURE = process.env.NODE_ENV === "production" && process.env.INSECURE_COOKIES !== "1";
export const SESSION_COOKIE = SECURE ? "__Host-cc_session" : "cc_session";
export const PENDING_COOKIE = SECURE ? "__Host-cc_pending" : "cc_pending";
export const SSO_COOKIE = SECURE ? "__Host-cc_sso" : "cc_sso";   // estado del inicio de sesión único en curso (firmado por la API, 10 min)

export const cookieOpts = (maxAge: number) => ({ httpOnly: true, sameSite: "strict" as const, secure: SECURE, path: "/", maxAge });

export function setCookie(res: NextResponse, name: string, value: string, maxAge: number) {
  res.cookies.set(name, value, cookieOpts(maxAge));
}
export function clearCookie(res: NextResponse, name: string) {
  res.cookies.set(name, "", cookieOpts(0));
}

/** El IdP vuelve a nuestro sitio con una navegación entre sitios: esa cookie (y solo esa) debe ser Lax para llegar al callback.
 *  La cookie de sesión sigue siendo Strict. */
export function setFlowCookie(res: NextResponse, value: string, maxAge: number) {
  res.cookies.set(SSO_COOKIE, value, { httpOnly: true, sameSite: "lax", secure: SECURE, path: "/", maxAge });
}

/** URL absoluta de nuestro propio sitio para redirigir (detrás de un proxy, `req.url` puede traer el host interno). */
export function appUrl(req: NextRequest, path: string): URL {
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host");
  const proto = req.headers.get("x-forwarded-proto")?.split(",")[0]?.trim() ?? (SECURE ? "https" : "http");
  return new URL(path, host ? `${proto}://${host}` : req.url);
}

const NO_STORE = { "Cache-Control": "no-store" };

export function json(body: unknown, status = 200, extra: Record<string, string> = {}) {
  return NextResponse.json(body, { status, headers: { ...NO_STORE, ...extra } });
}

/** Defensa CSRF además de SameSite=Strict: toda petición que cambia estado debe venir de nuestro propio origen. */
export function sameOrigin(req: NextRequest): boolean {
  const origin = req.headers.get("origin");
  if (!origin) return false;
  try {
    const host = new URL(origin).host;
    return host === req.headers.get("host") || host === req.headers.get("x-forwarded-host");
  } catch { return false; }
}

/** IP y user-agent del navegador para el registro de sesiones y los límites por IP.
 *  Se toma la ÚLTIMA entrada de X-Forwarded-For (la que añade tu proxy de confianza); las anteriores las controla el cliente. */
export function clientHeaders(req: NextRequest): Record<string, string> {
  const h: Record<string, string> = {};
  const xff = req.headers.get("x-forwarded-for");
  const ip = (xff ? xff.split(",").pop() : req.headers.get("x-real-ip"))?.trim();
  if (ip) h["X-Forwarded-For"] = ip;
  const ua = req.headers.get("user-agent");
  if (ua) h["User-Agent"] = ua.slice(0, 300);
  return h;
}

export async function callApi(path: string, init: { method?: string; body?: string; token?: string; req?: NextRequest } = {}) {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init.req ? clientHeaders(init.req) : {}) };
  if (init.token) headers.Authorization = `Bearer ${init.token}`;
  return fetch(`${API}/api/v1/${path}`, { method: init.method ?? "GET", headers, body: init.body, cache: "no-store", signal: AbortSignal.timeout(20000) });
}

/** Reenvía la respuesta de la API conservando estado, cuerpo y Retry-After. */
export async function relay(r: Response) {
  const retry = r.headers.get("retry-after");
  const type = r.headers.get("content-type") ?? "application/json";
  const disposition = r.headers.get("content-disposition");
  // Descargas (informes PDF/Excel): el cuerpo se reenvía como bytes; leerlo como texto lo corrompería.
  const binary = !/^(application\/json|text\/)/i.test(type) && r.status < 300;
  const body = r.status === 204 || r.status === 205 ? null : binary ? await r.arrayBuffer() : await r.text();
  return new NextResponse(body, {
    status: r.status,
    headers: { ...NO_STORE, "Content-Type": type, "X-Content-Type-Options": "nosniff",
               ...(disposition ? { "Content-Disposition": disposition } : {}), ...(retry ? { "Retry-After": retry } : {}) },
  });
}

/** Solo para mostrar el usuario de la sesión de desarrollo (JWT HS256): la API sigue validando la firma en cada llamada. */
export function devClaims(token: string): { email?: string; role?: string } | null {
  try { return JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8")); } catch { return null; }
}
