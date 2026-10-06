"use client";
import { useCallback, useEffect, useState } from "react";
import PageHead from "@/components/PageHead";
import { ApiError, api, authApi, fmtDate } from "@/lib/api";
import { ROLES, ROLE_HELP, ROLE_LABEL, type Me } from "@/lib/roles";

type Member = { id: string; email: string; full_name: string; role: string; verified: boolean; mfa: boolean; last_login_at: string | null; disabled_at: string | null };
type Invite = { id: string; email: string; role: string; created_at: string; expires_at: string };

const msgOf = (e: unknown) => (e instanceof ApiError ? e.message : "No pudimos conectar con el servidor.");

export default function Team() {
  const [me, setMe] = useState<Me | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("VIEWER");
  const [err, setErr] = useState("");
  const [ok, setOk] = useState<{ text: string; link: string | null } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [m, i] = await Promise.all([api<Member[]>("auth/members"), api<Invite[]>("auth/invitations")]);
      setMembers(m); setInvites(i);
    } catch (e: unknown) { setErr(msgOf(e)); }
  }, []);
  useEffect(() => { authApi<Me>("me").then(setMe).catch(() => null); load(); }, [load]);

  const invite = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(""); setOk(null);
    try {
      const r = await api<{ dev_link?: string | null }>("auth/invitations", { method: "POST", body: { email, role } });
      setOk({ text: `Invitación enviada a ${email}. Vale 7 días.`, link: r.dev_link ?? null }); setEmail(""); await load();
    } catch (e: unknown) { setErr(msgOf(e)); } finally { setBusy(false); }
  };
  const patch = async (id: string, body: { role?: string; disabled?: boolean }) => {
    setErr("");
    try { await api(`auth/members/${id}`, { method: "PATCH", body }); await load(); } catch (e: unknown) { setErr(msgOf(e)); await load(); }
  };
  const revoke = async (id: string) => { try { await api(`auth/invitations/${id}`, { method: "DELETE" }); await load(); } catch (e: unknown) { setErr(msgOf(e)); } };

  if (me && (me.role !== "ADMIN" || me.mode)) {
    return (<><PageHead title="Equipo" /><p className="note warn">Solo las personas con rol de administración pueden gestionar el equipo.</p></>);
  }
  return (
    <>
      <PageHead title="Equipo" sub="Quién tiene acceso a esta organización y qué puede hacer. Cada cambio queda en la auditoría." />
      {err && <p className="note bad" role="alert" style={{ marginBottom: 18 }}>{err}</p>}
      <div className="card">
        <table>
          <thead><tr><th>Persona</th><th>Rol</th><th>Dos pasos</th><th>Último acceso</th><th><span className="sr-only">Acciones</span></th></tr></thead>
          <tbody>
            {members.map((m) => {
              const mine = m.id === me?.id;
              return (
                <tr key={m.id}>
                  <td><b>{m.full_name}</b> {mine && <span className="badge me">Tú</span>} {m.disabled_at && <span className="badge bad">Deshabilitada</span>}{!m.verified && <span className="badge warn">Sin confirmar</span>}<div className="muted">{m.email}</div></td>
                  <td style={{ minWidth: 170 }}>
                    <select value={m.role} disabled={mine} onChange={(e) => patch(m.id, { role: e.target.value })} aria-label={`Rol de ${m.full_name}`}>
                      {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
                    </select>
                  </td>
                  <td>{m.mfa ? <span className="badge ok">Activo</span> : <span className="muted">No</span>}</td>
                  <td className="muted">{m.last_login_at ? fmtDate(m.last_login_at) : "Nunca"}</td>
                  <td style={{ textAlign: "right" }}>{!mine && <button className="secondary" onClick={() => patch(m.id, { disabled: !m.disabled_at })}>{m.disabled_at ? "Habilitar" : "Deshabilitar"}</button>}</td>
                </tr>
              );
            })}
            {members.length === 0 && <tr><td colSpan={5} className="empty">Cargando…</td></tr>}
          </tbody>
        </table>
      </div>

      <h2>Invitar a alguien</h2>
      <div className="card">
        <form className="row" onSubmit={invite} style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: "2 1 240px" }}><label htmlFor="inv-email">Correo</label>
            <input id="inv-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoCapitalize="none" /></div>
          <div className="field" style={{ flex: "1 1 180px" }}><label htmlFor="inv-role">Rol</label>
            <select id="inv-role" value={role} onChange={(e) => setRole(e.target.value)}>{ROLES.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}</select></div>
          <button type="submit" disabled={busy || !email}>{busy ? "Enviando…" : "Enviar invitación"}</button>
        </form>
        <p className="hint" style={{ marginTop: 10 }}>{ROLE_HELP[role]}. La persona crea su cuenta con el enlace; no necesita confirmar el correo aparte.</p>
        {ok && <p className="note ok" role="status" style={{ marginTop: 12 }}>{ok.text}{ok.link && <> Modo desarrollo, sin servidor de correo: <a href={ok.link}>enlace de la invitación</a>.</>}</p>}
        {invites.length > 0 && (
          <>
            <h3 style={{ marginTop: 24 }}>Invitaciones pendientes</h3>
            <ul className="list">
              {invites.map((i) => (
                <li key={i.id}>
                  <div className="t"><b>{i.email}</b><span>{ROLE_LABEL[i.role] ?? i.role}, vence {fmtDate(i.expires_at)}</span></div>
                  <button className="secondary" onClick={() => revoke(i.id)}>Anular</button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </>
  );
}
