import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, SSO_COOKIE, appUrl, callApi, setCookie, setFlowCookie } from "@/lib/server/bff";

export const dynamic = "force-dynamic";

// Inicio de sesión único, paso 2: el IdP vuelve aquí con ?code&state. La API valida el flujo (cookie firmada + state), canjea el código,
// valida el ID token y devuelve una sesión; el token se guarda en la cookie httpOnly de sesión. Los errores vuelven a /login con un
// código estable (la pantalla traduce el código: nunca refleja texto recibido en la URL).
export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams;
  const flow = req.cookies.get(SSO_COOKIE)?.value;

  const fail = (code: string) => {
    const res = NextResponse.redirect(appUrl(req, `/login?sso_error=${encodeURIComponent(code)}`), 303);
    res.headers.set("Cache-Control", "no-store");
    setFlowCookie(res, "", 0);
    return res;
  };

  if (q.get("error")) return fail(q.get("error") === "access_denied" ? "access_denied" : "idp_error");   // el texto del error del IdP se descarta
  const code = q.get("code");
  const state = q.get("state");
  if (!code || !state || !flow) return fail("invalid_flow");

  let data: { token?: string; expires_in?: number } = {};
  try {
    const r = await callApi("auth/sso/callback", { method: "POST", body: JSON.stringify({ code, state, flow_token: flow }), req });
    if (!r.ok) {
      const body = await r.json().catch(() => ({} as { code?: string }));
      return fail(typeof body.code === "string" && /^[a-z_]{1,40}$/.test(body.code) ? body.code : "error");
    }
    data = await r.json();
  } catch { return fail("unavailable"); }
  if (!data.token) return fail("error");

  // 200 con <meta refresh> y no un 302: un SameSite=Strict recién creado no se envía en una cadena de redirecciones iniciada por otro
  // sitio (el IdP). Con la navegación iniciada desde nuestra propia página la cookie de sesión viaja sin problema. Sin JavaScript inline (CSP).
  const html = `<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="robots" content="noindex">` +
    `<meta http-equiv="refresh" content="0;url=/"><title>Entrando…</title></head><body><p>Entrando… <a href="/">Continuar</a></p></body></html>`;
  const res = new NextResponse(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" } });
  setCookie(res, SESSION_COOKIE, data.token, data.expires_in ?? 0);
  setFlowCookie(res, "", 0);
  return res;
}
