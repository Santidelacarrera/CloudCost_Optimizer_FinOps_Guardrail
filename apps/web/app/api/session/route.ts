import { NextRequest, NextResponse } from "next/server";

const API = process.env.API_URL ?? "http://localhost:8000";
const DEV_LOGIN = (process.env.DEV_LOGIN ?? "dev") === "dev";

// Login de desarrollo: pide un token al backend y lo guarda en una cookie httpOnly (el navegador nunca lo ve en JS).
// En producción (AUTH_MODE=oidc) sustituye esta ruta por el flujo OIDC de tu proveedor.
export async function POST(req: NextRequest) {
  if (!DEV_LOGIN) return NextResponse.json({ detail: "Login de desarrollo deshabilitado" }, { status: 404 });
  const body = await req.json().catch(() => null);
  if (!body?.email || !body?.role) return NextResponse.json({ detail: "email y role requeridos" }, { status: 400 });
  const r = await fetch(`${API}/api/v1/dev/token`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: body.email, role: body.role }),
  });
  if (!r.ok) return NextResponse.json({ detail: "No se pudo iniciar sesión" }, { status: r.status });
  const { access_token } = await r.json();
  const res = NextResponse.json({ ok: true });
  res.cookies.set("cc_token", access_token, {
    httpOnly: true, sameSite: "strict", secure: process.env.NODE_ENV === "production" && process.env.INSECURE_COOKIES !== "1",
    path: "/", maxAge: 8 * 3600,
  });
  return res;
}

export async function DELETE() {
  const res = NextResponse.json({ ok: true });
  res.cookies.delete("cc_token");
  return res;
}
