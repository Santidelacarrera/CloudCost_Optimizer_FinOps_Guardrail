"use client";
import { Fragment, useState } from "react";
import { api, clp, money, moneyIn, pct1 } from "@/lib/api";
import PageHead from "@/components/PageHead";
import { DailyLine, MonthBars, MoversBars, ShareBar, Spark } from "@/components/SpendCharts";

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
type Breakdown = { name: string; total: string; share: string; months: Record<string, string> };
type Cloud = {
  filename: string; provider: string; format: string; currency: string; rows: number; granularity: "daily" | "monthly";
  first_day: string | null; last_day: string | null; total: string; credits: string; tax: string;
  months: { period: string; total: string; complete: boolean }[];
  by_service: Breakdown[]; by_region: Breakdown[]; by_account: Breakdown[]; by_group: Breakdown[];
  daily: { day: string; amount: string }[];
  movers: { name: string; previous: string; current: string; delta: string; pct: string | null }[];
  anomalies: { name: string; day: string; amount: string; baseline: string; excess: string }[];
  findings: Finding[]; notes: string[]; compared: string[] | null;
};
type Result = {
  cloud: Cloud[];
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

const isExcel = (f: File) => /\.xlsx$/i.test(f.name);

// Un .xlsx se envía como base64: el servidor lo lee con límites de tamaño y sin ejecutar fórmulas ni macros.
async function readBase64(f: File): Promise<string> {
  const bytes = new Uint8Array(await f.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

function CloudSection({ c }: { c: Cloud }) {
  const done = c.months.filter((m) => m.complete);
  const last = done[done.length - 1], prev = done[done.length - 2];
  const change = last && prev && Number(prev.total) > 0 ? (Number(last.total) - Number(prev.total)) / Number(prev.total) : null;
  const top = c.by_service[0];
  const sections = ([["Por servicio", c.by_service], ["Por región", c.by_region], ["Por cuenta / suscripción / proyecto", c.by_account], ["Por grupo de recursos", c.by_group]] as [string, Breakdown[]][])
    .filter(([, rows]) => rows.length > 0);
  const sev: Record<Finding["severity"], string> = { alert: "sp-n-alert", review: "sp-n-review", info: "sp-n-info" };
  return (
    <section className="sp-cloud" aria-label={`Análisis de nube de ${c.filename}`}>
      <h2>Nube · {c.filename}</h2>
      <p className="sp-meta">
        <span>{c.provider === "other" ? "Facturación" : c.provider.toUpperCase()}</span><span>{c.rows.toLocaleString("es-CL")} filas</span>
        <span>{c.granularity === "daily" ? "diario" : "mensual"}</span><span>{c.first_day} → {c.last_day}</span><span>moneda {c.currency}</span>
        {Number(c.credits) !== 0 && <span>créditos {moneyIn(c.credits, c.currency)}</span>}
        {Number(c.tax) !== 0 && <span>impuestos {moneyIn(c.tax, c.currency)}</span>}
      </p>

      <div className="totals">
        <div className="total"><div className="v">{moneyIn(c.total, c.currency)}</div><div className="l">gasto neto del archivo</div></div>
        <div className="total">
          <div className="v">{last ? moneyIn(last.total, c.currency) : "—"}</div>
          <div className="l">último mes completo{last ? ` (${last.period})` : ""}
            {change !== null && <span className={`sp-pill ${change > 0 ? "sp-up-t" : "sp-down-t"}`}>{change > 0 ? "▲ +" : "▼ −"}{Math.abs(change * 100).toFixed(1).replace(".", ",")} %</span>}</div>
        </div>
        <div className="total"><div className="v sp-small">{top ? top.name : "—"}</div><div className="l">{top ? `es el ${pct1(top.share)} del gasto` : "servicio que más pesa"}</div></div>
        <div className="total"><div className="v"><span className="mark">{c.findings.length}</span></div><div className="l">hallazgos para revisar</div></div>
      </div>

      <div className="card sp-card">
        <h3>Gasto por mes</h3>
        <p className="muted sp-sub">Solo se comparan meses completos; un mes cortado no se toma como una bajada.</p>
        <MonthBars months={c.months} compared={c.compared} currency={c.currency} />
      </div>

      {c.daily.length > 1 && (
        <div className="card sp-card">
          <h3>Gasto diario</h3>
          <p className="muted sp-sub">{c.anomalies.length > 0 ? "Los puntos rojos son días que costaron mucho más que la mediana de los días anteriores del mismo servicio." : "Sin picos diarios fuera de lo normal."}</p>
          <DailyLine daily={c.daily} spikes={c.anomalies} months={c.months} currency={c.currency} />
        </div>
      )}

      {(c.movers.length > 0 || c.by_service.length > 0) && (
        <div className="sp-two">
          {c.movers.length > 0 && (
            <div className="card sp-card">
              <h3>Qué cambió</h3>
              <p className="muted sp-sub">{c.compared?.[0]} → {c.compared?.[1]}, por servicio.</p>
              <MoversBars movers={c.movers} currency={c.currency} />
            </div>
          )}
          {c.by_service.length > 0 && (
            <div className="card sp-card">
              <h3>Dónde está el gasto</h3>
              <p className="muted sp-sub">Reparto del total por servicio.</p>
              <ShareBar parts={c.by_service} currency={c.currency} />
            </div>
          )}
        </div>
      )}

      {c.findings.length > 0 && (
        <div className="card sp-card">
          <h3>Qué revisar</h3>
          <ul className="sp-notes">
            {c.findings.map((f, k) => (
              <li key={k} className={sev[f.severity]}>
                <span className={`badge ${SEV[f.severity].cls}`}>{SEV[f.severity].label}</span>
                <div><b>{f.title}</b>{f.statement && <span className="muted"> · {f.statement}</span>}<p>{f.detail}</p></div>
                <span className="sp-amt">{f.amount ? moneyIn(f.amount, c.currency) : ""}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <details className="card sp-card sp-details">
        <summary>Ver las tablas con todos los datos</summary>
        <h3>Gasto por mes</h3>
        <table>
          <thead><tr><th>Mes</th><th style={{ textAlign: "right" }}>Gasto</th><th>Estado</th></tr></thead>
          <tbody>{c.months.map((m) => (
            <tr key={m.period}><td>{m.period}</td><td style={{ textAlign: "right" }}>{moneyIn(m.total, c.currency)}</td><td className={m.complete ? "muted" : "err"}>{m.complete ? "completo" : "incompleto (no se compara)"}</td></tr>
          ))}</tbody>
        </table>
        {sections.map(([title, rows]) => (
          <div key={title}>
            <h3>{title}</h3>
            <table>
              <thead><tr><th>Nombre</th><th>Tendencia</th><th style={{ textAlign: "right" }}>Total</th><th style={{ textAlign: "right" }}>% del total</th></tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.name}><td>{r.name}</td><td><Spark values={c.months.filter((m) => m.complete && r.months[m.period] !== undefined).map((m) => Number(r.months[m.period]))} /></td>
                  <td style={{ textAlign: "right" }}>{moneyIn(r.total, c.currency)}</td><td style={{ textAlign: "right" }} className="muted">{pct1(r.share)}</td></tr>
              ))}</tbody>
            </table>
          </div>
        ))}
      </details>
      {c.notes.map((n, k) => <p key={k} className="muted sp-note">⚠ {n}</p>)}
      <p className="muted sp-note">Esto es análisis de factura: dice dónde y cuándo cambió el gasto, no qué recurso lo causa ni cuánto se puede ahorrar. Para eso hay que conectar la cuenta o importar el inventario.</p>
    </section>
  );
}

export default function ExpensesPage() {
  const [files, setFiles] = useState<File[]>([]);
  const [res, setRes] = useState<Result | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [cols, setCols] = useState<Record<string, string>>({});

  const ownFindings = res ? res.total_findings - res.cloud.reduce((a, c) => a + c.findings.length, 0) : 0;

  const submit = async () => {
    if (!files.length) return;
    setBusy(true); setMsg(""); setRes(null);
    try {
      const payload = await Promise.all(files.map(async (f) => (
        isExcel(f) ? { filename: f.name, xlsx_base64: await readBase64(f) } : { filename: f.name, csv_text: await readText(f) })));
      const mapped = cols.date?.trim() && cols.cost?.trim() && cols.service?.trim()
        ? Object.fromEntries(Object.entries(cols).filter(([, v]) => v.trim()).map(([k, v]) => [k, v.trim()])) : undefined;
      if (Object.values(cols).some((v) => v.trim()) && !mapped) throw new Error("Para indicar tus columnas, rellena al menos fecha, costo y servicio.");
      setRes(await api<Result>("expenses/analyze", { method: "POST", body: { files: payload, columns: mapped } }));
    } catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  };

  return (
    <>
      <PageHead title="Analizar gastos" sub="Sube un CSV o Excel y recibe una revisión automática: si las cifras cuadran, cobros repetidos, gastos atípicos, avance de obra y gasto de nube." />
      <div className="card">
        <p>Sube un CSV o un Excel (.xlsx) y obtén una revisión automática. Reconoce <b>gastos comunes y presupuestos</b> (tablas planas o informes con secciones, subtotales y detalle por documento:
          si las cifras cuadran, cobros repetidos, posibles pagos dobles, cobros atípicos, dónde se concentra el gasto y, con varios meses, qué subió o apareció),{" "}
          <b>estados de pago de obra</b> (avance real vs proyectado, anticipo, atraso y proyección de término) y{" "}
          <b>exportaciones de facturación de nube</b> (AWS CUR o Cost Explorer, Azure Cost Management, GCP Billing: gasto por servicio, región y mes, qué creció, servicios nuevos y picos diarios).</p>
        <p className="muted">Los archivos .xls antiguos y los libros con macros no se aceptan: guárdalos como .xlsx o CSV.
          Puedes subir varios meses a la vez para compararlos. El contenido <b>no se guarda</b>: se analiza en memoria y en la auditoría solo quedan cifras agregadas.
          Los hallazgos son pistas para revisar, no conclusiones: que un gasto sobre o no depende de contexto que el archivo no trae.</p>
        <div className="grid" style={{ gridTemplateColumns: "1fr", maxWidth: 520 }}>
          <input type="file" multiple accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" aria-label="Archivos CSV o Excel de gastos"
                 onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
          <details className="sp-cols">
            <summary>Mi factura de nube tiene otras columnas</summary>
            <p className="muted" style={{ fontSize: 13, margin: "8px 0 0" }}>Escribe el nombre exacto de cada columna de tu archivo. Se aplica a todos los archivos de esta subida. Fecha, costo y servicio son obligatorios.</p>
            <div className="sp-cols-grid">
              {([["date", "Fecha"], ["cost", "Costo"], ["service", "Servicio o producto"], ["region", "Región (opcional)"], ["account", "Cuenta (opcional)"], ["currency", "Moneda (opcional)"]] as [string, string][]).map(([k, l]) => (
                <label key={k}>{l}<input value={cols[k] ?? ""} onChange={(e) => setCols({ ...cols, [k]: e.target.value })} autoComplete="off" /></label>
              ))}
            </div>
          </details>
          <button onClick={submit} disabled={!files.length || busy}>{busy ? "Analizando…" : `Analizar ${files.length > 1 ? `${files.length} archivos` : "archivo"}`}</button>
        </div>
        {msg && <p className="note bad" role="alert" style={{ marginTop: 14 }}>{msg}</p>}
      </div>

      {res && (
        <>
          {(res.statements.length > 0 || res.projects.length > 0) && <div className="totals">
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
            <div className="total"><div className="v"><span className="mark">{ownFindings}</span></div><div className="l">hallazgos para revisar</div></div>
          </div>}

          {(res.statements.length > 0 || res.projects.length > 0) && <><h2>Hallazgos</h2>
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
            {ownFindings > res.findings.length && <p className="muted">Se muestran los {res.findings.length} más relevantes de {ownFindings}.</p>}
          </div></>}

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

          {res.cloud.map((c, i) => <CloudSection key={`cloud${i}`} c={c} />)}

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
