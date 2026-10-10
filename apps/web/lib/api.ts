"use client";
// Todas las llamadas pasan por el BFF: /api/proxy (datos) y /api/auth (inicio de sesión). El token vive en una cookie httpOnly.
export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string, public retryAfter?: number, public errors?: string[]) { super(message); }
}

/** Sesión vencida o revocada: se limpia la cookie y se vuelve a la pantalla de acceso recordando a dónde iba. */
export async function expireSession() {
  if (typeof window === "undefined") return;
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => null);
  const here = window.location.pathname + window.location.search;
  window.location.href = `/login?expired=1${here !== "/" ? `&next=${encodeURIComponent(here)}` : ""}`;
}

async function request<T>(url: string, init?: { method?: string; body?: unknown }, opts?: { redirectOn401?: boolean }): Promise<T> {
  const r = await fetch(url, {
    method: init?.method ?? "GET",
    headers: { "Content-Type": "application/json" },
    body: init?.body === undefined ? undefined : JSON.stringify(init.body),
    cache: "no-store",
  });
  const text = await r.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* respuesta no JSON */ }
  if (r.status === 401 && opts?.redirectOn401) await expireSession();
  if (!r.ok) {
    const d = data?.detail;
    const msg =
      typeof d === "string" ? d
      : Array.isArray(d) ? d.map((x: any) => x.msg).join("; ")
      : d && typeof d === "object" ? [d.message, ...(Array.isArray(d.errors) ? d.errors : [])].filter(Boolean).join("\n")
      : `Error ${r.status}`;
    throw new ApiError(r.status, msg, data?.code, data?.retry_after ?? (Number(r.headers.get("retry-after")) || undefined),
                       Array.isArray(data?.errors) ? data.errors : undefined);
  }
  return data as T;
}

/** Datos de la aplicación (requieren sesión; si venció, vuelve a /login). */
export const api = <T,>(path: string, init?: { method?: string; body?: unknown }) => request<T>(`/api/proxy/${path}`, init, { redirectOn401: true });
/** Pantallas de acceso (login, registro, recuperación): un 401 aquí es un error normal, no una sesión vencida. */
export const authApi = <T,>(path: string, init?: { method?: string; body?: unknown }) => request<T>(`/api/auth/${path}`, init);

/** Descarga un archivo del API (informes PDF/Excel) con la sesión actual; los errores se muestran como texto, no como JSON suelto. */
export async function download(path: string, fallbackName: string) {
  const r = await fetch(`/api/proxy/${path}`, { cache: "no-store" });
  if (r.status === 401) { await expireSession(); throw new ApiError(401, "Sesión vencida"); }
  if (!r.ok) {
    let msg = `Error ${r.status}`;
    try { const d = (await r.json())?.detail; if (typeof d === "string") msg = d; } catch { /* sin cuerpo JSON */ }
    throw new ApiError(r.status, msg);
  }
  const name = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") ?? "")?.[1] ?? fallbackName;
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export const usd = (v: string | number | null | undefined) =>
  new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(v ?? 0));
export const pct = (v: string | number) => `${Math.round(Number(v) * 100)} %`;
export const fmtDate = (s: string) => new Date(s).toLocaleString("es-ES");
export const clp = (v: string | number | null | undefined) =>
  new Intl.NumberFormat("es-CL", { style: "currency", currency: "CLP", maximumFractionDigits: 0 }).format(Number(v ?? 0));
export const pct1 = (v: string | number) => `${(Number(v) * 100).toFixed(1).replace(".", ",")} %`;
export const money = (v: string | number | null | undefined, cur = "CLP") =>
  cur === "UF" ? `UF ${new Intl.NumberFormat("es-CL", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(v ?? 0))}` : clp(v);

/** Importe en cualquier moneda ISO (USD, EUR…); si la moneda no es válida, muestra el número con su código. */
export const moneyIn = (v: string | number | null | undefined, cur = "USD") => {
  try { return new Intl.NumberFormat("es-CL", { style: "currency", currency: cur, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(v ?? 0)); }
  catch { return `${cur} ${new Intl.NumberFormat("es-CL", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(v ?? 0))}`; }
};
