"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import PasswordField from "@/components/PasswordField";
import { ApiError, authApi } from "@/lib/api";
import { safeNext } from "@/lib/nav";
import { ROLES, ROLE_LABEL } from "@/lib/roles";

type Cfg = { local_enabled: boolean; dev_login_enabled: boolean };

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [wait, setWait] = useState(0);
  const [unverified, setUnverified] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "info" | "warn"; text: string } | null>(null);
  const [devLink, setDevLink] = useState<string | null>(null);
  const [cfg, setCfg] = useState<Cfg>({ local_enabled: true, dev_login_enabled: false });
  const [devEmail, setDevEmail] = useState("demo@example.com");
  const [devRole, setDevRole] = useState("FINOPS");

  useEffect(() => {
    authApi<Cfg>("config").then(setCfg).catch(() => null);
    const q = new URLSearchParams(window.location.search);
    if (q.get("expired")) setNotice({ kind: "warn", text: "Tu sesión terminó. Entra de nuevo para continuar." });
    else if (q.get("reset")) setNotice({ kind: "ok", text: "Contraseña actualizada. Ya puedes entrar con la nueva." });
    else if (q.get("verified")) setNotice({ kind: "ok", text: "Correo confirmado. Ya puedes entrar." });
  }, []);

  useEffect(() => {
    if (wait <= 0) return;
    const t = setInterval(() => setWait((w) => Math.max(0, w - 1)), 1000);
    return () => clearInterval(t);
  }, [wait]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(""); setUnverified(false); setDevLink(null); setNotice(null);
    try {
      const r = await authApi<{ status: string }>("login", { method: "POST", body: { email, password } });
      const next = new URLSearchParams(window.location.search).get("next");
      window.location.href = r.status === "mfa_required" ? `/mfa${next ? `?next=${encodeURIComponent(next)}` : ""}` : safeNext();
    } catch (e: unknown) {
      if (e instanceof ApiError) {
        if (e.status === 429) setWait(e.retryAfter ?? 60);
        if (e.code === "email_not_verified") setUnverified(true);
        setErr(e.message);
      } else setErr("No pudimos conectar con el servidor. Inténtalo de nuevo.");
      setPassword("");
    } finally { setBusy(false); }
  };

  const resend = async () => {
    try {
      const r = await authApi<{ dev_link?: string | null }>("resend-verification", { method: "POST", body: { email } });
      setErr(""); setUnverified(false); setDevLink(r.dev_link ?? null);
      setNotice({ kind: "ok", text: "Si el correo está registrado, te enviamos un enlace nuevo. Revisa también el spam." });
    } catch (e: unknown) { setErr(e instanceof ApiError ? e.message : "No pudimos reenviar el correo."); }
  };

  const devLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    const r = await fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: devEmail, role: devRole }) });
    if (r.ok) window.location.href = safeNext(); else setErr((await r.json().catch(() => ({}))).detail ?? "No se pudo entrar");
  };

  const locked = wait > 0;
  return (
    <div className="slip">
      <h1>Entra a tu cuenta</h1>
      <p className="sub">Revisa tus ahorros pendientes y aprueba los cambios.</p>
      {notice && <p className={`note ${notice.kind}`} role="status" style={{ marginBottom: 18 }}>{notice.text}</p>}
      {devLink && <p className="note info" style={{ marginBottom: 18 }}>Modo desarrollo, sin servidor de correo: <a href={devLink}>abrir el enlace de confirmación</a>.</p>}
      {cfg.local_enabled && (
        <form className="stack" onSubmit={submit} noValidate={false}>
          <div className="field">
            <label htmlFor="email">Correo</label>
            <input id="email" name="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required maxLength={254}
                   autoCapitalize="none" spellCheck={false} autoFocus />
          </div>
          <PasswordField label="Contraseña" value={password} onChange={setPassword} autoComplete="current-password" name="password" />
          {err && <p className="note bad" role="alert">{locked ? `Demasiados intentos. Vuelve a intentarlo en ${Math.floor(wait / 60)}:${String(wait % 60).padStart(2, "0")}.` : err}</p>}
          {unverified && <button type="button" className="secondary" onClick={resend}>Reenviar correo de confirmación</button>}
          <button type="submit" className="block" disabled={busy || locked || !email || !password}>{busy ? "Entrando…" : "Entrar"}</button>
          <div className="auth-links">
            <Link href="/forgot-password">Olvidé mi contraseña</Link>
            <span>¿Primera vez? <Link href="/signup">Crear cuenta</Link></span>
          </div>
        </form>
      )}
      {cfg.dev_login_enabled && (
        <details className="dev-box" open={!cfg.local_enabled}>
          <summary>Entrar como demostración (solo desarrollo)</summary>
          <form className="stack" onSubmit={devLogin}>
            <input type="email" value={devEmail} onChange={(e) => setDevEmail(e.target.value)} required aria-label="Correo de demostración" />
            <select value={devRole} onChange={(e) => setDevRole(e.target.value)} aria-label="Rol">{ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</select>
            <button type="submit" className="secondary">Entrar con datos de ejemplo</button>
          </form>
        </details>
      )}
    </div>
  );
}
