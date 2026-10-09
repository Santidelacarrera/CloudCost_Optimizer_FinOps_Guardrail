import { NextRequest, NextResponse } from "next/server";
import { appUrl, callApi, setFlowCookie } from "@/lib/server/bff";

export const dynamic = "force-dynamic";

// Inicio de sesión único, paso 1: la API prepara la URL del IdP (state, nonce y PKCE) y un token de flujo firmado.
// El token de flujo se guarda en una cookie httpOnly (nunca llega al JavaScript) y el navegador salta al IdP.
export async function GET(req: NextRequest) {
  const back = (code: string) => NextResponse.redirect(appUrl(req, `/login?sso_error=${code}`), 303);
  let data: { authorize_url?: string; flow_token?: string } = {};
  try {
    const r = await callApi("auth/sso/start", { req });
    if (!r.ok) return back(r.status === 404 ? "sso_disabled" : "unavailable");
    data = await r.json();
  } catch { return back("unavailable"); }
  if (!data.authorize_url || !data.flow_token) return back("unavailable");
  const res = NextResponse.redirect(data.authorize_url, 303);
  res.headers.set("Cache-Control", "no-store");
  setFlowCookie(res, data.flow_token, 600);
  return res;
}
