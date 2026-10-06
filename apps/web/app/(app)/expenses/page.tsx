"use client";
import { Fragment, useState } from "react";
import { api, clp, money, pct1 } from "@/lib/api";
import PageHead from "@/components/PageHead";

type Finding = { rule: string; severity: "alert" | "review" | "info"; title: string; detail: string; amount: string | null; statement: string | null };
type Item = { label: string; amount: string; share: string; note?: string | null; doc?: string | null; date?: string | null; group?: string | null };
type Group = { name: string; total: string; share: string; item_count: number };
type Section = { name: string; total: string; share: string; item_count: number; items: Item[]; groups?: Group[] };
type Check = { kind: string; label: string; section: string | null; declared: string; computed: string; ok: boolean };
type Statement = { filename: string; period: string | null; title: string | null; total: string; item_count: number; sections: Section[]; checks: Check[]; warnings: string[] };
type Month = { n: number; label: string; period: string; contract: string | null; projected: string | null; real: string | null };
type Project = {
  filename: string;
  summary: {
    currency: string; contract: string; last_ep?: number; last_month?: string; real_cumulative?: string; projected_cumulative?: string;
    real_progress?: string; projected_progress?: string; gap?: string; gap_share?: string; remaining?: string; eta?: string; planned_end?: string | null;
    months: Month[];
  };
  checks: { label: string; declared: string; computed: string; ok: boolean }[];
};
type Result = {
  statements: Statement[];
  projects: Project[];
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
      <PageHead title="Analizar gastos" sub="Sube un CSV y recibe una revisión automática: si las cifras cuadran, cobros repetidos, gastos atípicos y avance de obra." />
      <div className="card">
        <p>Sube un CSV y obtén una revisión automática. Reconoce <b>gastos comunes y presupuestos</b> (tablas planas o informes con secciones, subtotales y detalle por documento:
          si las cifras cuadran, cobros repetidos, posibles pagos dobles, cobros atípicos, dónde se concentra el gasto y, con varios meses, qué subió o apareció)
          y <b>estados de pago de obra</b> (avance real vs proyectado, anticipo, atraso y proyección de término).</p>
        <p className="muted">Si tu archivo es Excel, usa <i>Guardar como → CSV</i>.
          Puedes subir varios meses a la vez para compararlos. El contenido <b>no se guarda</b>: se analiza en memoria y en la auditoría solo quedan cifras agregadas.
          Los hallazgos son pistas para revisar, no conclusiones: que un gasto sobre o no depende de contexto que el archivo no trae.</p>
        <div className="grid" style={{ gridTemplateColumns: "1fr", maxWidth: 520 }}>
          <input type="file" multiple accept=".csv,text/csv" aria-label="Archivos CSV de gastos"
                 onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
          <button onClick={submit} disabled={!files.length || busy}>{busy ? "Analizando…" : `Analizar ${files.length > 1 ? `${files.length} archivos` : "archivo"}`}</button>
        </div>
        {msg && <p className="note bad" role="alert" style={{ marginTop: 14 }}>{msg}</p>}
      </div>

      {res && (
        <>
          <div className="totals">
            {res.projects.map((p, i) => (
              <div className="total" key={`p${i}`}>
                <div className="v">{money(p.summary.contract, p.summary.currency)}</div>
                <div className="l">contrato de obra · {p.filename}</div>
              </div>
            ))}
            {res.statements.map((s, i) => (
              <div className="total" key={i}>
                <div className="v">{clp(s.total)}</div>
                <div className="l">{s.period ?? s.filename} · {s.item_count} partidas</div>
              </div>
            ))}
            <div className="total">
              <div className={`v ${res.checks.failed ? "err" : ""}`}>{res.checks.passed} / {res.checks.passed + res.checks.failed}</div>
              <div className="l">subtotales y totales que cuadran</div>
            </div>
            <div className="total"><div className="v"><span className="mark">{res.total_findings}</span></div><div className="l">hallazgos para revisar</div></div>
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

          {res.projects.map((p, i) => {
            const sm = p.summary; const cur = sm.currency;
            let cr = 0, cp = 0;
            return (
              <div key={`proj${i}`}>
                <h2>Obra · {p.filename}</h2>
                {sm.real_cumulative && (
                  <div className="totals">
                    <div className="total"><div className="v">{pct1(sm.real_progress ?? 0)}</div><div className="l">avance real acumulado (EP {sm.last_ep} · {sm.last_month})</div></div>
                    <div className="total"><div className="v">{pct1(sm.projected_progress ?? 0)}</div><div className="l">avance proyectado a la misma fecha</div></div>
                    <div className="total"><div className={`v ${Number(sm.gap) > 0 ? "err" : ""}`}>{money(sm.gap, cur)}</div><div className="l">{Number(sm.gap) > 0 ? "atraso" : "adelanto"} ({pct1(Math.abs(Number(sm.gap_share ?? 0)))} del contrato)</div></div>
                    <div className="total"><div className="v">{money(sm.remaining, cur)}</div><div className="l">falta por ejecutar{sm.eta ? ` · término estimado ${sm.eta}` : ""}</div></div>
                  </div>
                )}
                <div className="card" style={{ overflowX: "auto" }}>
                  <table>
                    <thead><tr><th>EP</th><th>Mes</th><th style={{ textAlign: "right" }}>Proyectado</th><th style={{ textAlign: "right" }}>Real</th><th style={{ textAlign: "right" }}>Acum. proyectado</th><th style={{ textAlign: "right" }}>Acum. real</th><th style={{ textAlign: "right" }}>Diferencia</th></tr></thead>
                    <tbody>
                      {sm.months.filter((m) => m.n > 0).map((m) => {
                        const hasReal = m.real !== null && Number(m.real) > 0;
                        cp += Number(m.projected ?? 0); if (hasReal) cr += Number(m.real);
                        return (
                          <tr key={m.n}>
                            <td>{m.n}</td><td>{m.label}</td>
                            <td style={{ textAlign: "right" }}>{m.projected === null ? "—" : money(m.projected, cur)}</td>
                            <td style={{ textAlign: "right" }}>{hasReal ? money(m.real, cur) : "—"}</td>
                            <td style={{ textAlign: "right" }} className="muted">{m.projected === null ? "—" : money(cp, cur)}</td>
                            <td style={{ textAlign: "right" }} className="muted">{hasReal ? money(cr, cur) : "—"}</td>
                            <td style={{ textAlign: "right" }} className={hasReal && cp - cr > 0 ? "err" : "muted"}>{hasReal ? money(cp - cr, cur) : "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <p className="muted">Comprobaciones: {p.checks.filter((c) => c.ok).length} de {p.checks.length} cuadran.
                    {p.checks.filter((c) => !c.ok).map((c) => ` ✗ ${c.label} (${c.declared} de ${c.computed})`).join("")}</p>
                </div>
              </div>
            );
          })}

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
                        {sec.groups?.map((g) => (
                          <tr key={`${sec.name}-g-${g.name}`}><td style={{ paddingLeft: 12 }} className="muted">{g.name} ({g.item_count})</td><td style={{ textAlign: "right" }} className="muted">{clp(g.total)}</td><td style={{ textAlign: "right" }} className="muted">{pct1(g.share)}</td></tr>
                        ))}
                        {sec.items.map((it, k) => (
                          <tr key={`${sec.name}-${k}`}><td style={{ paddingLeft: 24 }}>{it.label}{it.note ? <span className="muted"> · {it.note}</span> : null}{it.doc ? <span className="muted"> · doc {it.doc}</span> : null}</td><td style={{ textAlign: "right" }}>{clp(it.amount)}</td><td style={{ textAlign: "right" }} className="muted">{pct1(it.share)}</td></tr>
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
