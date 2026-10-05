import { NextRequest, NextResponse } from "next/server";

const API = process.env.API_URL ?? "http://localhost:8000";

// BFF: reenvía a la API añadiendo el Bearer desde la cookie httpOnly. Solo rutas /api/v1/*.
async function handler(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const token = req.cookies.get("cc_token")?.value;
  if (!token) return NextResponse.json({ detail: "No autenticado" }, { status: 401 });
  if (path.some((p) => p === ".." || p === "." || p.includes("%"))) return NextResponse.json({ detail: "Ruta inválida" }, { status: 400 });
  if (req.method !== "GET" && req.headers.get("origin") && new URL(req.headers.get("origin")!).host !== req.headers.get("host"))
    return NextResponse.json({ detail: "Origen no permitido" }, { status: 403 });   // defensa CSRF adicional a SameSite=Strict
  const url = `${API}/api/v1/${path.join("/")}${req.nextUrl.search}`;
  const init: RequestInit = { method: req.method, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } };
  if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.text();
  const r = await fetch(url, init);
  return new NextResponse(await r.text(), { status: r.status, headers: { "Content-Type": r.headers.get("content-type") ?? "application/json" } });
}

export { handler as GET, handler as POST };
