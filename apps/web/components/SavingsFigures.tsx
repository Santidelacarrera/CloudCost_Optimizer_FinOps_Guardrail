import { fmtDate, pct, usd } from "@/lib/api";
import type { Baseline, SavingsFigures as Figures, SavingsVerification } from "@/lib/types";

// Las tres cifras de ahorro NUNCA se mezclan: lo que se proyecta (estimado), lo que vio quien aprobó (aprobado, congelado) y lo que se midió
// después con los costos reales (observado). Cada una dice de dónde sale y qué tan firme es.
const ATTRIBUTION: Record<string, { label: string; tone: string; text: string }> = {
  confirmed: { label: "Confirmado", tone: "ok", text: "El ahorro medido coincide con lo esperado una vez descontados el uso y el período." },
  partial: { label: "Parcial", tone: "warn", text: "Se midió un ahorro, pero menor que el esperado." },
  exceeds_model: { label: "Mayor que el modelo", tone: "warn", text: "Se ahorró más de lo estimado: conviene revisar si hubo otras causas (menos uso, otro cambio)." },
  not_applied: { label: "No se aplicó", tone: "bad", text: "El costo no bajó: el cambio no parece haberse aplicado." },
  confounded: { label: "Confuso", tone: "warn", text: "Cambió el uso u otros servicios en el mismo período: el ahorro no se puede atribuir con seguridad." },
  inconclusive: { label: "No concluyente", tone: "warn", text: "Faltan días de datos o la señal es demasiado débil para decidir." },
  unverified: { label: "Sin verificar", tone: "", text: "Todavía no hay una medición con costos posteriores al cambio." },
};
const GRADE: Record<string, string> = { billing: "facturación real", model: "modelo de precios", declared: "declarado a mano" };
const CONF: Record<string, string> = { high: "alta", medium: "media", low: "baja" };
const PHASE: Record<string, string> = { pre: "Antes del cambio", post: "Después del cambio", approval: "Al aprobar" };

const day = (s: string) => new Date(`${s.slice(0, 10)}T00:00:00`).toLocaleDateString("es-CL");
const asText = (x: unknown) => (typeof x === "string" ? x : typeof x === "object" && x !== null ? Object.values(x as Record<string, unknown>).filter((v) => typeof v === "string").join(" · ") || JSON.stringify(x) : String(x));

export default function SavingsFigures({ figures, verification, baselines }: { figures: Figures; verification: SavingsVerification | null; baselines: Baseline[] }) {
  const observed = figures.observed_monthly !== null && figures.observed_monthly !== undefined;
  const att = ATTRIBUTION[figures.observed_attribution ?? "unverified"] ?? ATTRIBUTION.unverified;
  const real = verification?.realization_pct;
  return (
    <section className="sf" aria-label="Ahorro: estimado, aprobado y observado">
      <h2>Ahorro: lo que se estimó, lo que se aprobó y lo que se midió</h2>
      <div className="sf-grid">
        <div className="sf-col">
          <div className="sf-k">1 · Estimado</div>
          <div className="sf-v">{figures.estimated_monthly != null ? usd(figures.estimated_monthly) : "—"}<small>/mes</small></div>
          <p>Proyección vigente con la fórmula de la regla. Puede cambiar en cada escaneo.</p>
        </div>
        <div className="sf-col">
          <div className="sf-k">2 · Aprobado</div>
          <div className="sf-v">{figures.approved_monthly != null ? usd(figures.approved_monthly) : "—"}{figures.approved_monthly != null && <small>/mes</small>}</div>
          <p>{figures.approved_at ? `Lo que vio quien aprobó, congelado el ${fmtDate(figures.approved_at)}. Es la base para medir.` : "Aún no se aprobó: se congela cuando se completan las aprobaciones."}</p>
        </div>
        <div className={`sf-col ${observed ? "sf-obs" : ""}`}>
          <div className="sf-k">3 · Observado</div>
          <div className="sf-v">{observed ? usd(figures.observed_monthly as number | string) : "—"}{observed && <small>/mes</small>}</div>
          <p>{observed ? <><span className={`badge ${att.tone}`}>{att.label}</span> {att.text}</> : "Se mide después de desplegar, comparando costos reales antes y después."}</p>
        </div>
      </div>

      {verification && (
        <div className="card sf-detail">
          <div className="sf-chips">
            {real != null && <span><b>{pct(Number(real) / 100)}</b> de lo esperado</span>}
            <span>datos: <b>{GRADE[verification.data_grade] ?? verification.data_grade}</b></span>
            <span>confianza: <b>{CONF[verification.confidence_grade] ?? verification.confidence_grade}</b></span>
            {verification.window_start && <span>ventana {day(verification.window_start)} → {verification.window_end ? day(verification.window_end) : "—"}</span>}
          </div>
          <table>
            <tbody>
              <tr><td className="muted">Costo mensual antes</td><td>{usd(verification.baseline_monthly_cost)}</td></tr>
              <tr><td className="muted">Costo mensual después</td><td>{usd(verification.observed_monthly_cost)}</td></tr>
              {verification.raw_observed_monthly_savings != null && <tr><td className="muted">Diferencia directa (sin descontar uso)</td><td>{usd(verification.raw_observed_monthly_savings)}</td></tr>}
              <tr><td className="muted">Ahorro observado (controlado por uso y período)</td><td><b>{usd(verification.observed_monthly_savings)}</b></td></tr>
            </tbody>
          </table>
          {verification.confounders.length > 0 && <><b>Qué más cambió en ese período</b><ul>{verification.confounders.map((c, i) => <li key={i}>{asText(c)}</li>)}</ul></>}
          {verification.limitations.length > 0 && <><b>Límites de esta medición</b><ul>{verification.limitations.map((c, i) => <li key={i}>{asText(c)}</li>)}</ul></>}
        </div>
      )}

      {baselines.length > 0 && (
        <details className="card sf-detail">
          <summary>Líneas base registradas ({baselines.length})</summary>
          <table>
            <thead><tr><th>Momento</th><th>Ventana</th><th style={{ textAlign: "right" }}>Costo mensual</th><th>Datos</th><th>Origen</th></tr></thead>
            <tbody>{baselines.map((b) => (
              <tr key={b.id}><td>{PHASE[b.phase] ?? b.phase}</td><td>{b.window_start ? `${day(b.window_start)} → ${b.window_end ? day(b.window_end) : "—"}` : "—"} <span className="muted">({b.days_with_data}/{b.days_expected} días)</span></td>
                <td style={{ textAlign: "right" }}>{usd(b.monthly_cost)}</td><td>{GRADE[b.data_grade] ?? b.data_grade}</td><td className="muted">{b.cost_source}</td></tr>
            ))}</tbody>
          </table>
        </details>
      )}
    </section>
  );
}
