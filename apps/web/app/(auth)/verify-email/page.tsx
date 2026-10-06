"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, authApi } from "@/lib/api";

type State = "ready" | "busy" | "ok" | "bad";

export default function VerifyEmail() {
  const [token, setToken] = useState<string | null>(null);
  const [state, setState] = useState<State>("ready");
  const [msg, setMsg] = useState("");
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState<{ devLink: string | null } | null>(null);

  useEffect(() => { setToken(new URLSearchParams(window.location.search).get("token") ?? ""); }, []);

  // Se confirma al pulsar el botón, no al abrir la página: los antivirus y previsualizadores de correo abren los enlaces y lo consumirían.
  const confirm = async () => {
    setState("busy");
    try { await authApi("verify-email", { method: "POST", body: { token } }); setState("ok"); }
    catch (e: unknown) { setMsg(e instanceof ApiError ? e.message : "No pudimos conectar con el servidor."); setState("bad"); }
  };
  const resend = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const r = await authApi<{ dev_link?: string | null }>("resend-verification", { method: "POST", body: { email } });
      setSent({ devLink: r.dev_link ?? null });
    } catch (e: unknown) { setMsg(e instanceof ApiError ? e.message : "No pudimos reenviar el correo."); }
  };

  if (state === "ok") {
    return (
      <div className="slip">
        <h1>Correo confirmado</h1>
        <p className="sub">Tu cuenta está activa. Ya puedes entrar.</p>
        <Link className="btn block" href="/login?verified=1">Iniciar sesión</Link>
      </div>
    );
  }
  if (state === "bad" || token === "") {
    return (
      <div className="slip">
        <h1>Este enlace ya no sirve</h1>
        <p className="sub">{msg || "Falta el código de confirmación."} Pide uno nuevo con tu correo.</p>
        {sent ? (
          <>
            <p className="note ok" role="status">Si el correo está registrado, te enviamos un enlace nuevo.</p>
            {sent.devLink && <p className="note info" style={{ marginTop: 12 }}>Modo desarrollo: <a href={sent.devLink}>abrir el enlace de confirmación</a>.</p>}
          </>
        ) : (
          <form className="stack" onSubmit={resend}>
            <div className="field"><label htmlFor="email">Correo</label>
              <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" autoCapitalize="none" /></div>
            <button type="submit">Enviarme un enlace nuevo</button>
          </form>
        )}
      </div>
    );
  }
  return (
    <div className="slip">
      <h1>Confirma tu correo</h1>
      <p className="sub">Un último paso para activar tu cuenta.</p>
      <button className="block" onClick={confirm} disabled={!token || state === "busy"}>{state === "busy" ? "Confirmando…" : "Confirmar mi correo"}</button>
    </div>
  );
}
