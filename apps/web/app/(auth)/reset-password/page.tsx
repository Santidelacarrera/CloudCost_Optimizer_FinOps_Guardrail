"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import PasswordField, { checkPassword } from "@/components/PasswordField";
import { ApiError, authApi } from "@/lib/api";

export default function Reset() {
  const [token, setToken] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [invalid, setInvalid] = useState(false);

  useEffect(() => { setToken(new URLSearchParams(window.location.search).get("token") ?? ""); }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErrors([]);
    try {
      await authApi("reset-password", { method: "POST", body: { token, password } });
      window.location.href = "/login?reset=1";
    } catch (e: unknown) {
      if (e instanceof ApiError && e.code === "invalid_token") setInvalid(true);
      else setErrors(e instanceof ApiError ? (e.errors?.length ? e.errors : [e.message]) : ["No pudimos conectar con el servidor."]);
    } finally { setBusy(false); }
  };

  if (invalid || token === "") {
    return (
      <div className="slip">
        <h1>Este enlace ya no sirve</h1>
        <p className="sub">Los enlaces de recuperación valen 1 hora y se usan una sola vez.</p>
        <Link className="btn block" href="/forgot-password">Pedir un enlace nuevo</Link>
      </div>
    );
  }
  const strong = checkPassword(password).rules.every((r) => r.ok);
  return (
    <div className="slip">
      <h1>Elige una contraseña nueva</h1>
      <p className="sub">Al cambiarla cerramos tus sesiones abiertas. Si tienes verificación en dos pasos, se sigue pidiendo.</p>
      <form className="stack" onSubmit={submit}>
        <PasswordField label="Contraseña nueva" value={password} onChange={setPassword} autoComplete="new-password" meter />
        {errors.length > 0 && <div className="note bad" role="alert">{errors.join("\n")}</div>}
        <button type="submit" className="block" disabled={busy || !strong || !token}>{busy ? "Guardando…" : "Guardar contraseña"}</button>
      </form>
    </div>
  );
}
