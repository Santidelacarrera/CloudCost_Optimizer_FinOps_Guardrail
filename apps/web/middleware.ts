import { NextRequest, NextResponse } from "next/server";
import { buildCsp, newNonce } from "./lib/csp";

// Rutas públicas. Todo lo demás exige cookie de sesión; la validez real la decide la API en cada llamada (aquí solo se evita pintar la
// interfaz a quien no tiene sesión). Se aceptan ambos nombres de cookie: con HTTPS lleva el prefijo __Host-.
const PUBLIC = ["/login", "/signup", "/forgot-password", "/reset-password", "/verify-email", "/terms", "/privacy", "/api/auth", "/api/session"];
const has = (req: NextRequest, name: string) => Boolean(req.cookies.get(`__Host-${name}`)?.value || req.cookies.get(name)?.value);

export function middleware(req: NextRequest) {
  // CSP con nonce propio de esta petición. Next lee el nonce de la cabecera de la PETICIÓN para marcar sus scripts; la de la
  // RESPUESTA es la que aplica el navegador.
  const nonce = newNonce();
  const csp = buildCsp(nonce);
  const forward = new Headers(req.headers);
  forward.set("x-nonce", nonce);
  forward.set("Content-Security-Policy", csp);
  const withCsp = (res: NextResponse) => { res.headers.set("Content-Security-Policy", csp); return res; };
  const next = () => withCsp(NextResponse.next({ request: { headers: forward } }));

  const { pathname, search } = req.nextUrl;
  if (PUBLIC.some((p) => pathname === p || pathname.startsWith(`${p}/`))) return next();
  if (pathname === "/mfa") return has(req, "cc_pending") ? next() : withCsp(NextResponse.redirect(new URL("/login", req.url)));
  if (!has(req, "cc_session")) {
    if (pathname.startsWith("/api/")) return withCsp(NextResponse.json({ detail: "No autenticado" }, { status: 401 }));
    const to = new URL("/login", req.url);
    if (pathname !== "/") to.searchParams.set("next", pathname + search);
    return withCsp(NextResponse.redirect(to));
  }
  return next();
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg).*)"] };
