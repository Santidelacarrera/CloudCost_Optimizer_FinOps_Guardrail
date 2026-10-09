import { usd } from "@/lib/api";
import type { ProjectionPoint, SavingsProjection } from "@/lib/types";

// Gráfico propio en SVG (sin librerías): «Gasto actual» frente a «Gasto optimizado», estilo hoja de libro mayor.
// Se distingue por trazo y rotulado directo, no solo por color; los datos exactos están en la tabla desplegable.
const W = 760, H = 300, M = { l: 64, r: 18, t: 16, b: 34 };

function niceMax(v: number): { max: number; step: number } {
  if (v <= 0) return { max: 1, step: 0.25 };
  const raw = v / 4, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  return { max: step * Math.ceil(v / step), step };
}
const label = (m: string) => {
  const [y, mo] = m.split("-").map(Number);
  return new Date(y, mo - 1, 1).toLocaleDateString("es-ES", { month: "short", year: "2-digit" }).replace(".", "");
};
const compact = (v: number) => new Intl.NumberFormat("es-ES", { notation: "compact", maximumFractionDigits: 1 }).format(v);

export default function SavingsChart({ data }: { data: SavingsProjection }) {
  const pts = data.points;
  const top = Math.max(1, ...pts.flatMap((p) => [p.actual, p.current].filter((v): v is number => v != null)));
  const { max, step } = niceMax(top * 1.05);
  const x = (i: number) => M.l + (pts.length === 1 ? 0 : (i * (W - M.l - M.r)) / (pts.length - 1));
  const y = (v: number) => M.t + (1 - v / max) * (H - M.t - M.b);
  const line = (pick: (p: ProjectionPoint) => number | null) => {
    let d = "", pen = false;
    pts.forEach((p, i) => { const v = pick(p); if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `; pen = true; });
    return d.trim();
  };
  const fi = pts.findIndex((p) => p.current != null && p.actual != null);       // mes en curso: unión entre historial y proyección
  const fut = pts.map((p, i) => ({ p, i })).filter(({ p }) => p.current != null && p.optimized != null);
  const band = fut.length > 1
    ? `${fut.map(({ p, i }, k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(p.current!).toFixed(1)}`).join(" ")} ${[...fut].reverse().map(({ p, i }) => `L${x(i).toFixed(1)},${y(p.optimized!).toFixed(1)}`).join(" ")} Z`
    : "";
  const last = pts[pts.length - 1];
  const ticks = Array.from({ length: Math.round(max / step) + 1 }, (_, k) => k * step);
  const every = Math.ceil(pts.length / 8);
  const summary = `Gasto mensual actual ${usd(data.baseline_monthly)}; con las recomendaciones abiertas bajaría a ${usd(last.optimized ?? data.baseline_monthly)} `
    + `en ${label(last.month)}. Ahorro acumulado en ${data.months_ahead} meses: hasta ${usd(data.cumulative_savings.optimized)}.`;

  return (
    <figure className="chart" data-testid="savings-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary} preserveAspectRatio="xMidYMid meet">
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid" x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} />
            <text className="axis" x={M.l - 8} y={y(t) + 4} textAnchor="end">{compact(t)}</text>
          </g>
        ))}
        {pts.map((p, i) => (i % every === 0 || i === pts.length - 1) && (
          <text key={p.month} className="axis" x={x(i)} y={H - 10} textAnchor="middle">{label(p.month)}</text>
        ))}
        {fi >= 0 && <line className="now" x1={x(fi)} x2={x(fi)} y1={M.t} y2={H - M.b} />}
        {band && <path className="gap" d={band} />}
        <path className="l-actual" d={line((p) => p.actual)} />
        <path className="l-current" d={line((p) => p.current)} />
        <path className="l-expected" d={line((p) => p.expected)} />
        <path className="l-optimized" d={line((p) => p.optimized)} />
        {pts.map((p, i) => p.optimized != null && i === pts.length - 1 && (
          <circle key="end" className="dot-opt" cx={x(i)} cy={y(p.optimized)} r={4}><title>{`${label(p.month)}: ${usd(p.optimized)}`}</title></circle>
        ))}
        {pts.map((p, i) => p.actual != null && p.current == null && (
          <circle key={p.month} className="dot-act" cx={x(i)} cy={y(p.actual)} r={3}><title>{`${label(p.month)}: ${usd(p.actual)}`}</title></circle>
        ))}
      </svg>
      <ul className="legend">
        <li><i className="k-actual" />Gasto actual (historial y línea base)</li>
        <li><i className="k-optimized" />Gasto optimizado (si se aplican todas)</li>
        <li><i className="k-expected" />Esperado (ponderado por confianza)</li>
      </ul>
      <details>
        <summary>Ver los datos y los supuestos</summary>
        <table>
          <thead><tr><th>Mes</th><th>Gasto real</th><th>Gasto actual</th><th>Gasto optimizado</th><th>Esperado</th></tr></thead>
          <tbody>
            {pts.map((p) => (
              <tr key={p.month}><td>{label(p.month)}</td>{[p.actual, p.current, p.optimized, p.expected].map((v, k) => <td key={k}>{v == null ? "—" : usd(v)}</td>)}</tr>
            ))}
          </tbody>
        </table>
        <ul className="assumptions">{data.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
      </details>
    </figure>
  );
}
