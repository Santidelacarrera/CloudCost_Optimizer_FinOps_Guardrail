"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, pct, usd } from "@/lib/api";
import type { Rec } from "@/lib/types";
import Badge from "@/components/Badge";

const STATUSES = ["", "PENDING_APPROVAL", "PROPOSED", "APPROVED", "PR_CREATED", "MERGED", "DEPLOYED", "VERIFIED", "REJECTED"];

export default function Recommendations() {
  const [items, setItems] = useState<Rec[]>([]);
  const [status, setStatus] = useState("");
  const [risk, setRisk] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    const q = new URLSearchParams({ limit: "100" });
    if (status) q.set("status", status);
    if (risk) q.set("risk", risk);
    api<{ items: Rec[] }>(`recommendations?${q}`).then((r) => setItems(r.items)).catch((e) => setErr(e.message));
  }, [status, risk]);
  return (
    <>
      <h1>Recomendaciones</h1>
      <div className="row" style={{ marginBottom: 12, maxWidth: 420 }}>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Estado">{STATUSES.map((s) => <option key={s} value={s}>{s || "Todos los estados"}</option>)}</select>
        <select value={risk} onChange={(e) => setRisk(e.target.value)} aria-label="Riesgo">{["", "LOW", "MEDIUM", "HIGH"].map((s) => <option key={s} value={s}>{s || "Todo riesgo"}</option>)}</select>
      </div>
      {err && <p className="err">{err}</p>}
      <div className="card">
        <table>
          <thead><tr><th>Recurso</th><th>Acción</th><th>Entorno</th><th>Riesgo</th><th>Conf.</th><th>Estado</th><th>Ahorro/mes</th></tr></thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id}>
                <td><Link href={`/recommendations/${r.id}`}>{r.title}</Link><div className="muted">{r.resource_id}</div></td>
                <td>{r.action}{r.destructive && <> <Badge value="destructiva" kind="warn" /></>}</td>
                <td>{r.environment ?? "—"}</td><td><Badge value={r.risk} /></td><td>{pct(r.confidence)}</td>
                <td><Badge value={r.status} kind="" /></td><td>{usd(r.estimated_monthly_savings)}</td>
              </tr>
            ))}
            {items.length === 0 && <tr><td colSpan={7} className="muted">Sin resultados.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
