"use client";
import { useCallback, useEffect, useState } from "react";
import PageHead from "@/components/PageHead";
import PasswordField, { checkPassword } from "@/components/PasswordField";
import { ApiError, api, authApi, fmtDate } from "@/lib/api";
import { ROLE_LABEL, type Me } from "@/lib/roles";

type Session = { id: string; ip: string | null; user_agent: string | null; created_at: string; last_seen_at: string; current: boolean };

const device = (ua?: string | null) => {
  if (!ua) return "Dispositivo desconocido";
  const b = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Navegador";
  const o = /Windows/.test(ua) ? "Windows" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Mac OS X/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  return o ? `${b} en ${o}` : b;
};
const msgOf = (e: unknown) => (e instanceof ApiError ? (e.errors?.length ? e.errors.join("\n") : e.message) : "No pudimos conectar con el servidor.");
const refreshShell = () => window.dispatchEvent(new Event("cc:me-changed"));

export default function Account() {
  const [me, setMe] = useState<Me | null>(null);
  const load = useCallback(() => authApi<Me>("me").then(setMe).catch(() => null), []);
  useEffect(() => { load(); }, [load]);
  const changed = () => { load(); refreshShell(); };

  if (!me) return <p className="muted">Cargando…</p>;
  if (me.mode) {
    return (
      <>
        <PageHead title="Cuenta y seguridad" />
        <p className="note info">Estás usando la sesión de demostración (AUTH_MODE=dev). Crea una cuenta real desde la pantalla de acceso para gestionar contraseña, verificación en dos pasos y sesiones.</p>
      </>
    );
  }
  return (
    <>
      <PageHead title="Cuenta y seguridad" sub="Tu contraseña, la verificación en dos pasos y los dispositivos donde tienes la sesión abierta." />
      <section className="section">
        <header><h2>Tu cuenta</h2><p>Estos datos los ve tu equipo en las aprobaciones.</p></header>
        <dl className="kv">
          <dt>Nombre</dt><dd>{me.full_name}</dd>
          <dt>Correo</dt><dd>{me.email}</dd>
          <dt>Organización</dt><dd>{me.organization}</dd>
          <dt>Rol</dt><dd>{ROLE_LABEL[me.role] ?? me.role}</dd>
          {me.last_login_at && <><dt>Último acceso</dt><dd>{fmtDate(me.last_login_at)}</dd></>}
          {me.password_changed_at && <><dt>Contraseña cambiada</dt><dd>{fmtDate(me.password_changed_at)}</dd></>}
        </dl>
      </section>
      {me.sso ? (
        <section className="section">
          <header><h2>Inicio de sesión único</h2><p>Tu acceso lo gestiona el proveedor de identidad de tu organización.</p></header>
          <p className="note info" style={{ maxWidth: 560 }}>
            La contraseña y la verificación en dos pasos se administran allí. Tu rol en CloudCost se actualiza en cada inicio de sesión según tus grupos.
          </p>
        </section>
      ) : (
        <>
          <PasswordSection email={me.email} name={me.full_name} onDone={changed} />
          <MfaSection me={me} onChanged={changed} />
        </>
      )}
      <SessionsSection />
    </>
  );
}

function PasswordSection({ email, name, onDone }: { email: string; name: string; onDone: () => void }) {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState(false);
  const strong = checkPassword(next, [email, name]).rules.every((r) => r.ok);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(""); setOk(false);
    try { await api("auth/change-password", { method: "POST", body: { current_password: cur, new_password: next } }); setOk(true); setCur(""); setNext(""); onDone(); }
    catch (e: unknown) { setErr(msgOf(e)); } finally { setBusy(false); }
  };
  return (
    <section className="section">
      <header><h2>Contraseña</h2><p>Al cambiarla cerramos tus otras sesiones. Esta se mantiene abierta.</p></header>
      <form className="stack" onSubmit={submit} style={{ maxWidth: 440 }}>
        <PasswordField label="Contraseña actual" value={cur} onChange={setCur} autoComplete="current-password" />
        <PasswordField label="Contraseña nueva" value={next} onChange={setNext} autoComplete="new-password" meter personal={[email, name]} />
        {err && <p className="note bad" role="alert">{err}</p>}
        {ok && <p className="note ok" role="status">Contraseña actualizada. Cerramos tus otras sesiones.</p>}
        <div><button type="submit" disabled={busy || !cur || !strong}>{busy ? "Guardando…" : "Cambiar contraseña"}</button></div>
      </form>
    </section>
  );
}

type Step = "idle" | "password" | "scan" | "codes" | "regen" | "disable";

function MfaSection({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const [step, setStep] = useState<Step>("idle");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [qr, setQr] = useState("");
  const [codes, setCodes] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const reset = () => { setStep("idle"); setPassword(""); setCode(""); setErr(""); setSetup(null); setQr(""); };
  const run = async (fn: () => Promise<void>) => { setBusy(true); setErr(""); try { await fn(); } catch (e: unknown) { setErr(msgOf(e)); } finally { setBusy(false); } };

  const start = () => run(async () => {
    const s = await api<{ secret: string; otpauth_uri: string }>("auth/mfa/setup", { method: "POST", body: { password } });
    const QRCode = (await import("qrcode")).default;
    setQr(await QRCode.toDataURL(s.otpauth_uri, { margin: 1, width: 400, errorCorrectionLevel: "M", color: { dark: "#16282b", light: "#ffffff" } }));
    setSetup(s); setPassword(""); setStep("scan");
  });
  const enable = () => run(async () => {
    const r = await api<{ recovery_codes: string[] }>("auth/mfa/enable", { method: "POST", body: { code } });
    setCodes(r.recovery_codes); setSaved(false); setCopied(false); setCode(""); setSetup(null); setQr(""); setStep("codes"); onChanged();
  });
  const regen = () => run(async () => {
    const r = await api<{ recovery_codes: string[] }>("auth/mfa/recovery-codes", { method: "POST", body: { password, code } });
    setCodes(r.recovery_codes); setSaved(false); setCopied(false); setPassword(""); setCode(""); setStep("codes"); onChanged();
  });
  const disable = () => run(async () => { await api("auth/mfa/disable", { method: "POST", body: { password, code } }); reset(); onChanged(); });

  const copy = async () => { await navigator.clipboard.writeText(codes.join("\n")).catch(() => null); setCopied(true); };
  const download = () => {
    const blob = new Blob([`CloudCost: códigos de recuperación de ${me.email}\nCada código sirve una sola vez.\n\n${codes.join("\n")}\n`], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "cloudcost-codigos-de-recuperacion.txt"; a.click(); URL.revokeObjectURL(a.href);
  };

  return (
    <section className="section" id="mfa">
      <header>
        <h2>Verificación en dos pasos</h2>
        <p>Además de la contraseña, pedimos un código que cambia cada 30 segundos desde tu teléfono. Si alguien descubre tu contraseña, no puede entrar.</p>
      </header>
      <div className="stack" style={{ maxWidth: 520 }}>
        {step === "idle" && !me.mfa_enabled && (
          <>
            {me.mfa_recommended && <p className="note warn">Tu rol puede aprobar cambios en producción. Te recomendamos activar este paso.</p>}
            <div><button onClick={() => setStep("password")}>Activar verificación en dos pasos</button></div>
          </>
        )}
        {step === "password" && (
          <form className="step" onSubmit={(e) => { e.preventDefault(); start(); }}>
            <b>Confirma que eres tú</b>
            <PasswordField label="Tu contraseña" value={password} onChange={setPassword} autoComplete="current-password" />
            {err && <p className="note bad" role="alert">{err}</p>}
            <div className="row"><button type="submit" disabled={busy || !password}>Continuar</button><button type="button" className="secondary" onClick={reset}>Cancelar</button></div>
          </form>
        )}
        {step === "scan" && setup && (
          <form className="step" onSubmit={(e) => { e.preventDefault(); enable(); }}>
            <ol>
              <li>Abre tu aplicación de autenticación (Google Authenticator, Microsoft Authenticator, 1Password, Authy…).</li>
              <li>Escanea este código o escribe la clave a mano.</li>
              <li>Escribe el código de 6 dígitos que muestra.</li>
            </ol>
            <div className="row" style={{ alignItems: "flex-start", gap: 20 }}>
              <div className="qr">{qr && <img src={qr} alt="Código QR para tu aplicación de autenticación" />}</div>
              <div className="stack" style={{ flex: 1, minWidth: 200 }}>
                <span className="hint">¿No puedes escanear? Escribe esta clave:</span>
                <div className="secret">{setup.secret.match(/.{1,4}/g)?.join(" ")}</div>
                <a href={setup.otpauth_uri} className="hint">Abrir en una aplicación de este dispositivo</a>
              </div>
            </div>
            <div className="field">
              <label htmlFor="otp">Código de 6 dígitos</label>
              <input id="otp" className="otp" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} placeholder="000000" />
            </div>
            {err && <p className="note bad" role="alert">{err}</p>}
            <div className="row"><button type="submit" disabled={busy || code.length !== 6}>{busy ? "Verificando…" : "Activar"}</button><button type="button" className="secondary" onClick={reset}>Cancelar</button></div>
          </form>
        )}
        {step === "codes" && (
          <div className="step">
            <b>Guarda tus códigos de recuperación</b>
            <p className="hint" style={{ margin: 0 }}>Si pierdes el teléfono, cada código te deja entrar una vez. No volveremos a mostrarlos: guárdalos en un gestor de contraseñas o impresos.</p>
            <div className="codes" role="list">{codes.map((c) => <span key={c} role="listitem">{c}</span>)}</div>
            <div className="row"><button className="secondary" onClick={copy}>{copied ? "Copiados" : "Copiar"}</button><button className="secondary" onClick={download}>Descargar .txt</button></div>
            <label className="row" style={{ gap: 10 }}><input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} /> Ya los guardé en un lugar seguro</label>
            <div><button disabled={!saved} onClick={reset}>Listo</button></div>
          </div>
        )}
        {step === "idle" && me.mfa_enabled && (
          <>
            <p className="note ok">Activa. Te quedan <b>{me.recovery_codes_left ?? 0}</b> códigos de recuperación sin usar.</p>
            <div className="row">
              <button className="secondary" onClick={() => setStep("regen")}>Generar códigos nuevos</button>
              <button className="secondary" onClick={() => setStep("disable")}>Desactivar</button>
            </div>
          </>
        )}
        {(step === "regen" || step === "disable") && (
          <form className="step" onSubmit={(e) => { e.preventDefault(); (step === "regen" ? regen : disable)(); }}>
            <b>{step === "regen" ? "Generar códigos de recuperación nuevos" : "Desactivar la verificación en dos pasos"}</b>
            <p className="hint" style={{ margin: 0 }}>{step === "regen" ? "Los códigos anteriores dejarán de funcionar." : "Tu cuenta quedará protegida solo por la contraseña."} Confirma con tu contraseña y un código.</p>
            <PasswordField label="Tu contraseña" value={password} onChange={setPassword} autoComplete="current-password" />
            <div className="field"><label htmlFor="c2">Código de la aplicación o de recuperación</label>
              <input id="c2" value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" maxLength={16} autoCapitalize="none" spellCheck={false} /></div>
            {err && <p className="note bad" role="alert">{err}</p>}
            <div className="row">
              <button type="submit" className={step === "disable" ? "danger" : undefined} disabled={busy || !password || code.length < 6}>{step === "regen" ? "Generar" : "Desactivar"}</button>
              <button type="button" className="secondary" onClick={reset}>Cancelar</button>
            </div>
          </form>
        )}
      </div>
    </section>
  );
}

function SessionsSection() {
  const [items, setItems] = useState<Session[]>([]);
  const [err, setErr] = useState("");
  const load = useCallback(() => { api<Session[]>("auth/sessions").then(setItems).catch((e) => setErr(msgOf(e))); }, []);
  useEffect(() => { load(); }, [load]);
  const revoke = async (id: string) => { try { await api(`auth/sessions/${id}`, { method: "DELETE" }); load(); } catch (e: unknown) { setErr(msgOf(e)); } };
  const others = async () => { try { await api("auth/sessions/revoke-others", { method: "POST", body: {} }); load(); } catch (e: unknown) { setErr(msgOf(e)); } };
  return (
    <section className="section">
      <header><h2>Sesiones activas</h2><p>Cierra las que no reconozcas. Las sesiones también vencen solas por inactividad.</p></header>
      <div style={{ maxWidth: 640 }}>
        {err && <p className="note bad" role="alert">{err}</p>}
        <ul className="list">
          {items.map((s) => (
            <li key={s.id}>
              <div className="t">
                <b>{device(s.user_agent)} {s.current && <span className="badge me">Esta sesión</span>}</b>
                <span>{s.ip ?? "IP desconocida"}, activa por última vez {fmtDate(s.last_seen_at)}</span>
              </div>
              {!s.current && <button className="secondary" onClick={() => revoke(s.id)}>Cerrar</button>}
            </li>
          ))}
        </ul>
        {items.length > 1 && <div style={{ marginTop: 12 }}><button className="secondary" onClick={others}>Cerrar todas las demás</button></div>}
      </div>
    </section>
  );
}
