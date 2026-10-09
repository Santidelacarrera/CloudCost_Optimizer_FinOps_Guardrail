import { NextRequest } from "next/server";
import { SESSION_COOKIE, callApi, json, relay, sameOrigin } from "@/lib/server/bff";

// BFF: reenvía a la API añadiendo el Bearer desde la cookie httpOnly. Solo rutas /api/v1/*.
// Los pasos que emiten tokens van por /api/auth (guardan la cookie); aquí se bloquean para que ningún token vuelva al navegador.
const BLOCKED = new Set(["auth/login", "auth/mfa/verify", "auth/logout", "auth/signup", "auth/verify-email", "auth/resend-verification",
                         "auth/forgot-password", "auth/reset-password", "auth/passkey/login", "auth/passkey/login/options", "auth/mfa/passkey",
                         "auth/mfa/passkey/options", "dev/token"]);

async function handler(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  if (!token) return json({ detail: "No autenticado" }, 401);
  if (path.some((p) => p === ".." || p === "." || p.includes("%") || p.includes("\\"))) return json({ detail: "Ruta inválida" }, 400);
  if (BLOCKED.has(path.join("/"))) return json({ detail: "No encontrado" }, 404);
  if (req.method !== "GET" && !sameOrigin(req)) return json({ detail: "Origen no permitido" }, 403);    // defensa CSRF además de SameSite=Strict
  const hasBody = req.method !== "GET" && req.method !== "HEAD" && req.method !== "DELETE";
  return relay(await callApi(`${path.join("/")}${req.nextUrl.search}`, { method: req.method, token, req, body: hasBody ? await req.text() : undefined }));
}

export { handler as GET, handler as POST, handler as PATCH, handler as DELETE };
