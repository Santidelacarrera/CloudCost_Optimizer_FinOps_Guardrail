"use client";
import { Fragment, useState } from "react";
import { api, clp, pct1 } from "@/lib/api";

type Finding = { rule: string; severity: "alert" | "review" | "info"; title: string; detail: string; amount: string | null; statement: string | null };
type Item = { label: string; amount: string; share: string };
type Section = { name: string; total: string; share: string; item_count: number; items: Item[] };
type Check = { kind: string; label: string; section: string | null; declared: string; computed: string; ok: boolean };
type Statement = { filename: string; period: string | null; title: string | null; total: string; item_count: number; sections: Section[]; checks: Check[]; warnings: string[] };
type Result = {
  statements: Statement[];
  findings: Finding[];
  total_findings: number;
  checks: { passed: number; failed: number };
  comparison: { periods: string[]; totals: string[]; rows: { label: string; amounts: (string | null)[] }[] } | null;
};

const SEV: Record<Finding["severity"], { label: string; cls: string }> = {
  alert: { label: "Revisar primero", cls: "bad" },
  review: { label: "Verificar", cls: "warn" },
  info: { label: "Contexto", cls: "" },
};

// Los CSV exportados desde Excel en Windows suelen venir en Windows-1252, no en UTF-8.
async function readText(f: File): Promise<string> {
  const buf = await f.arrayBuffer();
  try { return new TextDecoder("utf-8", { fatal: true }).decode(buf); }
  catch { return new TextDecoder("windows-1252").decode(buf); }
}

export default function ExpensesPage() {
  const [files, setFiles] = useState<File[]>([]);
  const [res, setRes] = useState<Result | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!files.length) return;
    setBusy(true); setMsg(""); setRes(null);
    try {
      const payload = await Promise.all(files.map(async (f) => ({ filename: f.name, csv_text: await readText(f) })));
      setRes(await api<Result>("expenses/analyze", { method: "POST", body: { files: payload } }));
    } catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  };

  return (
    <>
      <h1>Analizar gastos</h1>
      <div className="card">
        <p>Sube un CSV de gastos (gastos comunes, presupuesto, listado de costos) y obtén una revisión automática:
          si los subtotales cuadran, cobros repetidos, posibles pagos dobles, dónde se concentra el gasto y, si subes varios meses, qué subió o apareció.</p>
        <p className="muted">Sirve cualquier CSV con una descripción y un monto por fila (con o sin secciones y subtotales). Si tu archivo es Excel, usa <i>Guardar como → CSV</i>.
          Puedes subir varios meses a la vez para compararlos. El contenido <b>no se guarda</b>: se analiza en memoria y en la auditoría solo quedan cifras agregadas.
          Los hallazgos son pistas para revisar, no conclusiones: que un gasto sobre o no depende de contexto que el archivo no trae.</p>
        <div className="grid" style={{ gridTemplateColumns: "1fr", maxWidth: 520 }}>
          <input type="file" multiple accept=".csv,text/csv" aria-label="Archivos CSV de gastos"
                 onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
          <button onClick={submit} disabled={!files.length || busy}>{busy ? "Analizando…" : `Analizar ${files.length > 1 ? `${files.length} archivos` : "archivo"}`}</button>
        </div>
        {msg && <p className="err" style={{ whiteSpace: "pre-wrap" }}>{msg}</p>}
      </div>

      {res && (
        <>
          <div className="grid" style={{ margin: "12px 0" }}>
            {res.statements.map((s, i) => (
              <div className="card kpi" key={i}>
                <div className="v">{clp(s.total)}</div>
                <div className="l">{s.period ?? s.filename} · {s.item_count} partidas</div>
              </div>
            ))}
            <div className="card kpi">
              <div className={`v ${res.checks.failed ? "err" : ""}`}>{res.checks.passed} / {res.checks.passed + res.checks.failed}</div>
              <div className="l">subtotales y totales que cuadran</div>
            </div>
            <div className="card kpi"><div className="v">{res.total_findings}</div><div className="l">hallazgos para revisar</div></div>
          </div>

          <h2>Hallazgos</h2>
          <div className="card">
            {res.findings.length === 0 && <p className="muted">No se encontró nada que revisar con las reglas actuales.</p>}
            <table>
              <tbody>
                {res.findings.map((f, i) => (
                  <tr key={i}>
                    <td style={{ width: 120 }}><span className={`badge ${SEV[f.severity].cls}`}>{SEV[f.severity].label}</span></td>
                    <td><b>{f.title}</b>{f.statement && <span className="muted"> · {f.statement}</span>}<br /><span className="muted">{f.detail}</span></td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{f.amount ? <>{clp(f.amount)}<br /><span className="muted">monto involucrado</span></> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {res.total_findings > res.findings.length && <p className="muted">Se muestran los {res.findings.length} más relevantes de {res.total_findings}.</p>}
          </div>

          {res.comparison && (
            <>
              <h2>Comparación entre meses</h2>
              <div className="card" style={{ overflowX: "auto" }}>
                <table>
                  <thead><tr><th>Partida</th>{res.comparison.periods.map((p) => <th key={p} style={{ textAlign: "right" }}>{p}</th>)}</tr></thead>
                  <tbody>
                    {res.comparison.rows.map((r, i) => (
                      <tr key={i}><td>{r.label}</td>{r.amounts.map((a, j) => <td key={j} style={{ textAlign: "right" }}>{a === null ? "—" : clp(a)}</td>)}</tr>
                    ))}
                    <tr><td><b>Total</b></td>{res.comparison.totals.map((t, j) => <td key={j} style={{ textAlign: "right" }}><b>{clp(t)}</b></td>)}</tr>
                  </tbody>
                </table>
              </div>
            </>
          )}

          {res.statements.map((s, i) => (
            <div key={i}>
              <h2>Detalle {s.period ?? s.filename}</h2>
              <div className="card">
                {s.warnings.map((w, k) => <p key={k} className="muted">⚠ {w}</p>)}
                <table>
                  <thead><tr><th>Sección / partida</th><th style={{ textAlign: "right" }}>Monto</th><th style={{ textAlign: "right" }}>% del total</th></tr></thead>
                  <tbody>
                    {s.sections.map((sec) => (
                      <Fragment key={sec.name}>
                        <tr><td><b>{sec.name}</b></td><td style={{ textAlign: "right" }}><b>{clp(sec.total)}</b></td><td style={{ textAlign: "right" }}><b>{pct1(sec.share)}</b></td></tr>
                        {sec.items.map((it, k) => (
                          <tr key={`${sec.name}-${k}`}><td style={{ paddingLeft: 24 }}>{it.label}</td><td style={{ textAlign: "right" }}>{clp(it.amount)}</td><td style={{ textAlign: "right" }} className="muted">{pct1(it.share)}</td></tr>
                        ))}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
                {s.checks.some((c) => !c.ok) && (
                  <p className="err">No cuadran: {s.checks.filter((c) => !c.ok).map((c) => `${c.label}${c.section ? ` (${c.section})` : ""}: declarado ${clp(c.declared)}, suma ${clp(c.computed)}`).join("; ")}</p>
                )}
              </div>
            </div>
          ))}
        </>
      )}
    </>
  );
}
