"use client";
// Todas las llamadas pasan por el BFF (/api/proxy), que añade el token desde una cookie httpOnly.
export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) { super(message); }
}

export async function api<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const r = await fetch(`/api/proxy/${path}`, {
    method: init?.method ?? "GET",
    headers: { "Content-Type": "application/json" },
    body: init?.body === undefined ? undefined : JSON.stringify(init.body),
    cache: "no-store",
  });
  const text = await r.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* respuesta no JSON */ }
  if (r.status === 401 && typeof window !== "undefined") window.location.href = "/login";
  if (!r.ok) {
    const d = data?.detail;
    const msg =
      typeof d === "string" ? d
      : Array.isArray(d) ? d.map((x: any) => x.msg).join("; ")
      : d && typeof d === "object" ? [d.message, ...(Array.isArray(d.errors) ? d.errors : [])].filter(Boolean).join("\n")
      : `Error ${r.status}`;
    throw new ApiError(r.status, msg, data?.code);
  }
  return data as T;
}

export const usd = (v: string | number | null | undefined) =>
  new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(v ?? 0));
export const pct = (v: string | number) => `${Math.round(Number(v) * 100)} %`;
export const fmtDate = (s: string) => new Date(s).toLocaleString("es-ES");
export const clp = (v: string | number | null | undefined) =>
  new Intl.NumberFormat("es-CL", { style: "currency", currency: "CLP", maximumFractionDigits: 0 }).format(Number(v ?? 0));
export const pct1 = (v: string | number) => `${(Number(v) * 100).toFixed(1).replace(".", ",")} %`;
