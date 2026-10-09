// Política de seguridad de contenido con nonce por petición. Se construye en el middleware (una por respuesta) y Next agrega el nonce a
// sus propios scripts al renderizar de forma dinámica. Sin terceros, sin formularios hacia fuera, sin <base>, sin plugins.
//
// script-src: SIN 'unsafe-inline'. 'strict-dynamic' deja cargar los fragmentos que cargan los scripts con nonce; 'self' solo sirve de
// respaldo a navegadores antiguos (los modernos lo ignoran cuando hay 'strict-dynamic'). En desarrollo hace falta 'unsafe-eval' (React).
// style-src conserva 'unsafe-inline': la interfaz usa atributos style={{…}} de React, que un nonce no puede cubrir; inyectar estilos
// es un riesgo mucho menor que inyectar scripts.
export function buildCsp(nonce: string, dev = process.env.NODE_ENV !== "production"): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "form-action 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
}

/** 128 bits aleatorios en base64 (válido para 'nonce-…'). Web Crypto: funciona en el runtime Edge del middleware. */
export function newNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}
