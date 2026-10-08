// Utilidades del BFF (solo servidor). El navegador nunca ve el token de sesión: vive en una cookie httpOnly.
import { NextRequest, NextResponse } from "next/server";

export const API = process.env.API_URL ?? "http://localhost:8000";

// Con HTTPS se usa el prefijo __Host- (el navegador exige Secure, Path=/ y ningún Domain: la cookie no puede fijarse desde un subdominio).
export const SECURE = process.env.NODE_ENV === "production" && process.env.INSECURE_COOKIES !== "1";
export const SESSION_COOKIE = SECURE ? "__Host-cc_session" : "cc_session";
export const PENDING_COOKIE = SECURE ? "__Host-cc_pending" : "cc_pending";

export const cookieOpts = (maxAge: number) => ({ httpOnly: true, sameSite: "strict" as const, secure: SECURE, path: "/", maxAge });

export function setCookie(res: NextResponse, name: string, value: string, maxAge: number) {
  res.cookies.set(name, value, cookieOpts(maxAge));
}
export function clearCookie(res: NextResponse, name: string) {
  res.cookies.set(name, "", cookieOpts(0));
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
  // Los reportes (PDF/Excel) son binarios: leerlos como texto los corrompería.
  const binary = !/^(application\/json|text\/)/i.test(type);
  const body = r.status === 204 || r.status === 205 ? null : binary ? await r.arrayBuffer() : await r.text();
  return new NextResponse(body, {
    status: r.status,
    headers: { ...NO_STORE, "Content-Type": type, ...(disposition ? { "Content-Disposition": disposition } : {}), ...(retry ? { "Retry-After": retry } : {}) },
  });
}

/** Solo para mostrar el usuario de la sesión de desarrollo (JWT HS256): la API sigue validando la firma en cada llamada. */
export function devClaims(token: string): { email?: string; role?: string } | null {
  try { return JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8")); } catch { return null; }
}
