"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, usd } from "@/lib/api";
import type { Summary } from "@/lib/types";
import Badge from "@/components/Badge";
import PageHead from "@/components/PageHead";

type Scan = { id: string; status: string; stats: Record<string, number> | null; created_at: string; error: string | null };
const ACCOUNT = "22222222-2222-2222-2222-222222222222";   // cuenta demo del seed; en uso real, selecciónala desde /cloud-accounts
const REPO = "33333333-3333-3333-3333-333333333333";

export default function Dashboard() {
  const [s, setS] = useState<Summary | null>(null);
  const [scans, setScans] = useState<Scan[]>([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      setS(await api<Summary>("dashboard/summary"));
      setScans(await api<Scan[]>("scans?limit=5"));
    } catch (e: any) { setErr(e.message); }
  };
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);

  const scan = async () => {
    setBusy(true); setErr("");
    try { await api("scans", { method: "POST", body: { cloud_account_id: ACCOUNT, repository_id: REPO } }); await load(); }
    catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };

  return (
    <>
      <PageHead title="Panel" sub="Lo que podrías ahorrar este mes, lo que espera tu aprobación y lo que ya se verificó."
                actions={<>
                  <a className="btn secondary" href="/api/proxy/reports/waste?format=pdf" download>Reporte PDF</a>
                  <a className="btn secondary" href="/api/proxy/reports/waste?format=xlsx" download>Reporte Excel</a>
                  <button onClick={scan} disabled={busy}>{busy ? "Encolando…" : "Ejecutar escaneo"}</button>
                </>} />
      {err && <p className="note bad" role="alert" style={{ marginBottom: 18 }}>{err}</p>}
      {s && (
        <>
          <div className="totals">
            <div className="total"><div className="v"><span className="mark">{usd(s.potential_savings)}</span></div><div className="l">Ahorro potencial al mes ({s.savings_pct}% del gasto)</div></div>
            <div className="total"><div className="v">{usd(s.monthly_spend)}</div><div className="l">Gasto mensual inventariado</div></div>
            <div className="total"><div className="v">{s.pending_approval}</div><div className="l">Pendientes de aprobación</div></div>
            <div className="total"><div className={`v ${s.high_risk ? "err" : ""}`}>{s.high_risk}</div><div className="l">Con riesgo alto</div></div>
            <div className="total"><div className="v">{usd(s.realized_savings)}</div><div className="l">Ahorro verificado{s.realization_pct != null ? ` (${s.realization_pct}% de lo esperado)` : ""}</div></div>
          </div>
          <h2>Mayores oportunidades abiertas</h2>
          <div className="card">
            <table>
              <thead><tr><th>Recomendación</th><th>Estado</th><th>Riesgo</th><th>Ahorro/mes</th></tr></thead>
              <tbody>
                {s.top_recommendations.map((r) => (
                  <tr key={r.id}>
                    <td><Link href={`/recommendations/${r.id}`}>{r.title}</Link></td>
                    <td><Badge value={r.status} kind="" /></td><td><Badge value={r.risk} /></td><td>{usd(r.estimated_monthly_savings)}</td>
                  </tr>
                ))}
                {s.top_recommendations.length === 0 && <tr><td colSpan={4} className="empty">Sin oportunidades abiertas. Ejecuta un escaneo para buscarlas.</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
      <h2>Últimos escaneos</h2>
      <div className="card">
        <table>
          <thead><tr><th>Fecha</th><th>Estado</th><th>Resultado</th></tr></thead>
          <tbody>
            {scans.map((x) => (
              <tr key={x.id}><td>{new Date(x.created_at).toLocaleString("es-ES")}</td><td><Badge value={x.status} kind={x.status === "SUCCEEDED" ? "ok" : x.status === "FAILED" ? "bad" : ""} /></td>
                <td className="muted">{x.error ?? (x.stats ? `${x.stats.resources_seen ?? 0} recursos · ${x.stats.findings ?? 0} hallazgos` : "—")}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
