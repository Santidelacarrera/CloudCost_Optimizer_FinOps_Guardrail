/** Destino tras iniciar sesión: solo rutas internas (evita que `?next=` lleve a otro sitio). */
export function safeNext(): string {
  const n = new URLSearchParams(window.location.search).get("next") ?? "/";
  return n.startsWith("/") && !n.startsWith("//") && !n.startsWith("/\\") ? n : "/";
}
