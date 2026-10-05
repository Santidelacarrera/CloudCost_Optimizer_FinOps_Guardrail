"use client";
import { useState } from "react";

const ROLES = ["ADMIN", "FINOPS", "SRE", "DEVELOPER", "AUDITOR", "VIEWER"];

export default function Login() {
  const [email, setEmail] = useState("demo@example.com");
  const [role, setRole] = useState("FINOPS");
  const [err, setErr] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const r = await fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, role }) });
    if (r.ok) window.location.href = "/"; else setErr((await r.json().catch(() => ({}))).detail ?? "Error");
  };
  return (
    <div className="card" style={{ maxWidth: 380, margin: "80px auto" }}>
      <h1>CloudCost Optimizer</h1>
      <p className="muted">Acceso de desarrollo. En producción se usa OIDC (ver docs/security.md).</p>
      <form onSubmit={submit} className="grid" style={{ gridTemplateColumns: "1fr" }}>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required aria-label="Email" />
        <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Rol">{ROLES.map((r) => <option key={r}>{r}</option>)}</select>
        <button type="submit">Entrar</button>
        {err && <span className="err">{err}</span>}
      </form>
    </div>
  );
}
