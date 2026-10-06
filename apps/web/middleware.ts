import { NextRequest, NextResponse } from "next/server";

// Rutas públicas. Todo lo demás exige cookie de sesión; la validez real la decide la API en cada llamada (aquí solo se evita pintar la
// interfaz a quien no tiene sesión). Se aceptan ambos nombres de cookie: con HTTPS lleva el prefijo __Host-.
const PUBLIC = ["/login", "/signup", "/forgot-password", "/reset-password", "/verify-email", "/api/auth", "/api/session"];
const has = (req: NextRequest, name: string) => Boolean(req.cookies.get(`__Host-${name}`)?.value || req.cookies.get(name)?.value);

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  if (PUBLIC.some((p) => pathname === p || pathname.startsWith(`${p}/`))) return NextResponse.next();
  if (pathname === "/mfa") return has(req, "cc_pending") ? NextResponse.next() : NextResponse.redirect(new URL("/login", req.url));
  if (!has(req, "cc_session")) {
    if (pathname.startsWith("/api/")) return NextResponse.json({ detail: "No autenticado" }, { status: 401 });
    const to = new URL("/login", req.url);
    if (pathname !== "/") to.searchParams.set("next", pathname + search);
    return NextResponse.redirect(to);
  }
  return NextResponse.next();
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg).*)"] };
