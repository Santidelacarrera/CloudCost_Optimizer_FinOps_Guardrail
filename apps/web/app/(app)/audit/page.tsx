"use client";
import { useEffect, useState } from "react";
import { api, fmtDate } from "@/lib/api";
import type { AuditEvent } from "@/lib/types";
import PageHead from "@/components/PageHead";

export default function Audit() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [chain, setChain] = useState<{ ok: boolean; checked: number; first_bad_seq: number | null } | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    api<AuditEvent[]>("audit/events?limit=100").then(setEvents).catch((e) => setErr(e.message));
    api<{ ok: boolean; checked: number; first_bad_seq: number | null }>("audit/verify").then(setChain).catch(() => {});
  }, []);
  return (
    <>
      <PageHead title="Auditoría" sub="Registro encadenado con hashes: si alguien altera un evento, la cadena deja de cuadrar." />
      {chain && <p className={`note ${chain.ok ? "ok" : "bad"}`} style={{ marginBottom: 18 }}>{chain.ok ? `Cadena íntegra: ${chain.checked} eventos verificados.` : `Cadena alterada desde el evento #${chain.first_bad_seq}.`}</p>}
      {err && <p className="note bad" role="alert" style={{ marginBottom: 18 }}>{err}</p>}
      <div className="card">
        <table>
          <thead><tr><th>#</th><th>Fecha</th><th>Evento</th><th>Actor</th><th>Entidad</th><th>Hash</th></tr></thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.seq}><td>{e.seq}</td><td>{fmtDate(e.created_at)}</td><td>{e.event_type}</td><td>{e.actor_id ?? e.actor_type}</td>
                <td className="muted">{e.entity_type}</td><td className="muted" title={e.hash}>{e.hash.slice(0, 10)}…</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
