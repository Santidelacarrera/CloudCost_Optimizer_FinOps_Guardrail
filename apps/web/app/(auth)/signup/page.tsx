"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import PasswordField, { checkPassword } from "@/components/PasswordField";
import { ApiError, authApi } from "@/lib/api";

type Done = { kind: "check_email" | "created"; devLink: string | null };

export default function Signup() {
  const [name, setName] = useState("");
  const [org, setOrg] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<Done | null>(null);
  const [resent, setResent] = useState(false);

  useEffect(() => { setInvite(new URLSearchParams(window.location.search).get("invite")); }, []);

  const strong = checkPassword(password, [email, name]).rules.every((r) => r.ok);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErrors([]);
    try {
      const r = await authApi<{ status: "check_email" | "created"; dev_link?: string | null }>("signup", {
        method: "POST", body: { email, password, full_name: name, organization_name: invite ? null : org, invite_token: invite },
      });
      setDone({ kind: r.status, devLink: r.dev_link ?? null });
    } catch (e: unknown) {
      if (e instanceof ApiError) setErrors(e.errors?.length ? e.errors : [e.message]);
      else setErrors(["No pudimos conectar con el servidor. Inténtalo de nuevo."]);
    } finally { setBusy(false); }
  };

  const resend = async () => {
    try {
      const r = await authApi<{ dev_link?: string | null }>("resend-verification", { method: "POST", body: { email } });
      setResent(true); setDone((d) => (d ? { ...d, devLink: r.dev_link ?? d.devLink } : d));
    } catch (e: unknown) { setErrors([e instanceof ApiError ? e.message : "No pudimos reenviar el correo."]); }
  };

  if (done?.kind === "created") {
    return (
      <div className="slip">
        <h1>Cuenta creada</h1>
        <p className="sub">Ya formas parte del equipo. Entra con tu correo y tu contraseña.</p>
        <Link className="btn block" href="/login">Ir a iniciar sesión</Link>
      </div>
    );
  }
  if (done) {
    return (
      <div className="slip">
        <h1>Revisa tu correo</h1>
        <p className="sub">Si <b>{email}</b> no tenía una cuenta, te enviamos un enlace para confirmarlo. Vale 24 horas. Si ya tenías una, te avisamos por ahí.</p>
        {done.devLink && <p className="note info" style={{ marginBottom: 16 }}>Modo desarrollo, sin servidor de correo: <a href={done.devLink}>abrir el enlace de confirmación</a>.</p>}
        {resent && <p className="note ok" role="status" style={{ marginBottom: 16 }}>Enviamos otro correo. Revisa también el spam.</p>}
        {errors.map((m) => <p key={m} className="note bad" role="alert" style={{ marginBottom: 16 }}>{m}</p>)}
        <div className="stack">
          <button className="secondary" onClick={resend}>No llegó, reenviar</button>
          <Link href="/login">Volver a iniciar sesión</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="slip">
      <h1>{invite ? "Únete a tu equipo" : "Crea tu cuenta"}</h1>
      <p className="sub">{invite ? "Usa el mismo correo al que llegó la invitación." : "Eres quien administra la organización: después podrás invitar a tu equipo."}</p>
      <form className="stack" onSubmit={submit}>
        <div className="field">
          <label htmlFor="name">Tu nombre</label>
          <input id="name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" required maxLength={120} autoFocus />
        </div>
        {!invite && (
          <div className="field">
            <label htmlFor="org">Nombre de la organización</label>
            <input id="org" value={org} onChange={(e) => setOrg(e.target.value)} autoComplete="organization" required minLength={2} maxLength={120} />
          </div>
        )}
        <div className="field">
          <label htmlFor="email">Correo</label>
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required maxLength={254} autoCapitalize="none" spellCheck={false} />
        </div>
        <PasswordField label="Contraseña" value={password} onChange={setPassword} autoComplete="new-password" meter personal={[email, name]} />
        {errors.length > 0 && <div className="note bad" role="alert">{errors.join("\n")}</div>}
        <button type="submit" className="block" disabled={busy || !strong || !name || !email || (!invite && org.trim().length < 2)}>{busy ? "Creando…" : "Crear cuenta"}</button>
        <div className="auth-links"><span>¿Ya tienes cuenta? <Link href="/login">Inicia sesión</Link></span></div>
      </form>
    </div>
  );
}
