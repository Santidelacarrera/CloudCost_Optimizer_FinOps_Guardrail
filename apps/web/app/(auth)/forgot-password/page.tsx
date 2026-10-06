"use client";
import Link from "next/link";
import { useState } from "react";
import { ApiError, authApi } from "@/lib/api";

export default function Forgot() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState<{ devLink: string | null } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const r = await authApi<{ dev_link?: string | null }>("forgot-password", { method: "POST", body: { email } });
      setDone({ devLink: r.dev_link ?? null });
    } catch (e: unknown) { setErr(e instanceof ApiError ? e.message : "No pudimos conectar con el servidor."); }
    finally { setBusy(false); }
  };

  if (done) {
    return (
      <div className="slip">
        <h1>Revisa tu correo</h1>
        <p className="sub">Si <b>{email}</b> tiene una cuenta, te enviamos un enlace para elegir una contraseña nueva. Vale 1 hora y sirve una sola vez.</p>
        {done.devLink && <p className="note info" style={{ marginBottom: 16 }}>Modo desarrollo, sin servidor de correo: <a href={done.devLink}>abrir el enlace para cambiar la contraseña</a>.</p>}
        <Link href="/login">Volver a iniciar sesión</Link>
      </div>
    );
  }
  return (
    <div className="slip">
      <h1>Recupera tu acceso</h1>
      <p className="sub">Escribe tu correo y te enviamos un enlace para elegir una contraseña nueva.</p>
      <form className="stack" onSubmit={submit}>
        <div className="field"><label htmlFor="email">Correo</label>
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" autoCapitalize="none" spellCheck={false} autoFocus /></div>
        {err && <p className="note bad" role="alert">{err}</p>}
        <button type="submit" className="block" disabled={busy || !email}>{busy ? "Enviando…" : "Enviar enlace"}</button>
        <div className="auth-links"><Link href="/login">Volver a iniciar sesión</Link></div>
      </form>
    </div>
  );
}
