"use client";
import Link from "next/link";
import { useRef, useState } from "react";
import { ApiError, authApi } from "@/lib/api";
import { safeNext } from "@/lib/nav";

export default function Mfa() {
  const [mode, setMode] = useState<"app" | "recovery">("app");
  const [code, setCode] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [dead, setDead] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const send = async (value: string) => {
    setBusy(true); setErr("");
    try {
      await authApi("mfa/verify", { method: "POST", body: { code: value } });
      window.location.href = safeNext();
    } catch (e: unknown) {
      if (e instanceof ApiError) {
        setErr(e.message);
        if (e.code === "mfa_expired" || e.code === "locked") setDead(true);
      } else setErr("No pudimos conectar con el servidor. Inténtalo de nuevo.");
      setCode(""); input.current?.focus();
    } finally { setBusy(false); }
  };
  const onChange = (v: string) => {
    if (mode === "app") {
      const digits = v.replace(/\D/g, "").slice(0, 6);
      setCode(digits);
      if (digits.length === 6 && !busy) send(digits);          // se envía solo al completar los 6 dígitos
    } else setCode(v.slice(0, 16));
  };
  const cancel = async () => { await authApi("logout", { method: "POST" }).catch(() => null); window.location.href = "/login"; };

  if (dead) {
    return (
      <div className="slip">
        <h1>Vuelve a empezar</h1>
        <p className="sub">{err}</p>
        <Link className="btn block" href="/login">Ir a iniciar sesión</Link>
      </div>
    );
  }
  return (
    <div className="slip">
      <h1>Verificación en dos pasos</h1>
      <p className="sub">{mode === "app" ? "Escribe el código de 6 dígitos de tu aplicación de autenticación." : "Escribe uno de los códigos de recuperación que guardaste. Cada uno sirve una vez."}</p>
      <form className="stack" onSubmit={(e) => { e.preventDefault(); if (code) send(code); }}>
        <div className="field">
          <label htmlFor="code" className={mode === "app" ? "sr-only" : undefined}>{mode === "app" ? "Código de 6 dígitos" : "Código de recuperación"}</label>
          {mode === "app"
            ? <input ref={input} id="code" className="otp" inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]*" maxLength={6} value={code} onChange={(e) => onChange(e.target.value)} placeholder="000000" autoFocus />
            : <input ref={input} id="code" value={code} onChange={(e) => onChange(e.target.value)} placeholder="xxxxx-xxxxx" autoComplete="off" autoCapitalize="none" spellCheck={false} autoFocus />}
        </div>
        {err && <p className="note bad" role="alert">{err}</p>}
        <button type="submit" className="block" disabled={busy || (mode === "app" ? code.length !== 6 : code.length < 10)}>{busy ? "Verificando…" : "Verificar"}</button>
        <div className="auth-links">
          <button type="button" className="linklike" onClick={() => { setMode(mode === "app" ? "recovery" : "app"); setCode(""); setErr(""); }}>
            {mode === "app" ? "Usar un código de recuperación" : "Usar mi aplicación"}
          </button>
          <button type="button" className="linklike" onClick={cancel}>Cancelar</button>
        </div>
      </form>
    </div>
  );
}
