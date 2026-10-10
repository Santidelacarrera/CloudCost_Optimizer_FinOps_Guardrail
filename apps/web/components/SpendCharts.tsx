"use client";
import { useEffect, useId, useRef, useState } from "react";
import { moneyIn } from "@/lib/api";

// Gráficos del análisis de facturación de nube, en SVG propio (sin librerías) y con el lenguaje de la hoja de libro mayor del producto:
// bolígrafo azul para lo que se mide, lápiz rojo para lo que sube o llama la atención, trama rayada para lo incompleto.
// Nunca solo color: los aumentos llevan «+», los meses incompletos llevan trama y rótulo, y cada gráfico tiene su tabla al lado.

const compact = (v: number) => new Intl.NumberFormat("es-ES", { notation: "compact", maximumFractionDigits: 1 }).format(v);
const monthLabel = (m: string) => {
  const [y, mo] = m.split("-").map(Number);
  return new Date(y, mo - 1, 1).toLocaleDateString("es-ES", { month: "short", year: "2-digit" }).replace(".", "");
};
const dayLabel = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString("es-ES", { day: "numeric", month: "short" }).replace(".", "");

function niceMax(v: number): { max: number; step: number } {
  if (v <= 0) return { max: 1, step: 0.25 };
  const raw = v / 4, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  return { max: step * Math.ceil(v / step), step };
}

// Ancho real del contenedor: así el texto de los ejes mide lo mismo en un móvil que en un monitor (no se encoge con el SVG).
function useWidth(initial = 760): [React.RefObject<HTMLElement | null>, number] {
  const ref = useRef<HTMLElement | null>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(300, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

type Tip = { x: number; y: number; title: string; lines: string[] } | null;

function Tooltip({ tip, width }: { tip: Tip; width: number }) {
  if (!tip) return null;
  const left = Math.min(Math.max((tip.x / width) * 100, 12), 88);
  return (
    <div className="sp-tip" style={{ left: `${left}%`, top: tip.y }} role="presentation">
      <b>{tip.title}</b>
      {tip.lines.map((l, i) => <span key={i}>{l}</span>)}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ gasto por mes
export type MonthPoint = { period: string; total: string; complete: boolean };

export function MonthBars({ months, compared, currency }: { months: MonthPoint[]; compared: string[] | null; currency: string }) {
  const uid = useId().replace(/:/g, "");
  const [tip, setTip] = useState<Tip>(null);
  const [ref, W] = useWidth();
  const H = 270, M = { l: 50, r: 12, t: 44, b: 34 };
  const vals = months.map((m) => Number(m.total));
  const { max, step } = niceMax(Math.max(1, ...vals) * 1.05);
  const n = months.length;
  const slot = (W - M.l - M.r) / Math.max(n, 1);
  const bw = Math.min(88, slot * 0.62);
  const x = (i: number) => M.l + slot * i + (slot - bw) / 2;
  const y = (v: number) => M.t + (1 - v / max) * (H - M.t - M.b);
  const ticks = Array.from({ length: Math.round(max / step) + 1 }, (_, k) => k * step);
  const pair = compared ? compared.map((p) => months.findIndex((m) => m.period === p)) : [];
  const summary = `Gasto por mes: ${months.map((m) => `${monthLabel(m.period)} ${moneyIn(m.total, currency)}${m.complete ? "" : " (incompleto)"}`).join("; ")}.`;

  return (
    <figure className="sp-fig" ref={ref as React.RefObject<HTMLElement & HTMLDivElement>} onMouseLeave={() => setTip(null)}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary}>
        <defs>
          <pattern id={`${uid}-hatch`} width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="7" height="7" fill="var(--sheet)" />
            <line x1="0" y1="0" x2="0" y2="7" stroke="var(--pen)" strokeWidth="2" opacity=".55" />
          </pattern>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line className="sp-grid" x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} />
            <text className="sp-axis" x={M.l - 8} y={y(t) + 4} textAnchor="end">{compact(t)}</text>
          </g>
        ))}
        {pair.length === 2 && pair[0] >= 0 && pair[1] >= 0 && (
          <g className="sp-bracket">
            <path d={`M${x(pair[0])},${M.t - 14} V${M.t - 22} H${x(pair[1]) + bw} V${M.t - 14}`} />
            <text x={(x(pair[0]) + x(pair[1]) + bw) / 2} y={M.t - 28} textAnchor="middle">{W < 460 ? "se comparan" : "meses que se comparan"}</text>
          </g>
        )}
        {months.map((m, i) => {
          const v = Number(m.total), top = y(Math.max(v, 0)), h = Math.max(1, y(0) - top);
          return (
            <g key={m.period}
               onMouseMove={() => setTip({ x: x(i) + bw / 2, y: top - 8, title: monthLabel(m.period),
                 lines: [moneyIn(m.total, currency), m.complete ? "mes completo" : "mes incompleto: no se compara"] })}>
              <rect x={x(i) - 4} y={M.t} width={bw + 8} height={H - M.t - M.b} fill="transparent" />
              <path className={m.complete ? "sp-bar" : "sp-bar sp-bar-open"} style={m.complete ? undefined : { fill: `url(#${uid}-hatch)` }}
                    d={`M${x(i)},${y(0)} V${top + 4} Q${x(i)},${top} ${x(i) + 4},${top} H${x(i) + bw - 4} Q${x(i) + bw},${top} ${x(i) + bw},${top + 4} V${y(0)} Z`} />
              <text className="sp-val" x={x(i) + bw / 2} y={top - 7} textAnchor="middle">{compact(v)}</text>
              <text className="sp-axis" x={x(i) + bw / 2} y={H - 12} textAnchor="middle">{monthLabel(m.period)}{m.complete ? "" : " ·"}</text>
              {!m.complete && <text className="sp-open" x={x(i) + bw / 2} y={H - 1} textAnchor="middle">incompleto</text>}
            </g>
          );
        })}
      </svg>
      <Tooltip tip={tip} width={W} />
    </figure>
  );
}

// ------------------------------------------------------------------------------------------------ gasto diario
export type DayPoint = { day: string; amount: string };
export type Spike = { name: string; day: string; amount: string; baseline: string; excess: string };

export function DailyLine({ daily, spikes, months, currency }: { daily: DayPoint[]; spikes: Spike[]; months: MonthPoint[]; currency: string }) {
  const uid = useId().replace(/:/g, "");
  const [tip, setTip] = useState<Tip>(null);
  const [hover, setHover] = useState<number | null>(null);
  const [ref, W] = useWidth();
  if (daily.length < 2) return null;
  const H = 250, M = { l: 50, r: 12, t: 26, b: 32 };
  const t0 = new Date(`${daily[0].day}T00:00:00`).getTime(), t1 = new Date(`${daily[daily.length - 1].day}T00:00:00`).getTime();
  const span = Math.max(1, t1 - t0);
  const tx = (d: string) => M.l + ((new Date(`${d}T00:00:00`).getTime() - t0) / span) * (W - M.l - M.r);
  const vals = daily.map((d) => Number(d.amount));
  const { max, step } = niceMax(Math.max(1, ...vals) * 1.08);
  const y = (v: number) => M.t + (1 - v / max) * (H - M.t - M.b);
  const ticks = Array.from({ length: Math.round(max / step) + 1 }, (_, k) => k * step);
  const line = daily.map((d, i) => `${i ? "L" : "M"}${tx(d.day).toFixed(1)},${y(Number(d.amount)).toFixed(1)}`).join(" ");
  const area = `${line} L${tx(daily[daily.length - 1].day).toFixed(1)},${y(0)} L${tx(daily[0].day).toFixed(1)},${y(0)} Z`;
  const open = months.filter((m) => !m.complete).map((m) => m.period);
  const spikeDays = new Set(spikes.map((s) => s.day));
  const xs = Array.from({ length: Math.min(W < 460 ? 3 : 6, daily.length) }, (_, k) => daily[Math.round((k * (daily.length - 1)) / Math.max(1, Math.min(W < 460 ? 3 : 6, daily.length) - 1))]);
  const sx = (e: React.MouseEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * (W - M.l - M.r) + M.l;
    let best = 0, bd = Infinity;
    daily.forEach((d, i) => { const dist = Math.abs(tx(d.day) - px); if (dist < bd) { bd = dist; best = i; } });
    setHover(best);
    setTip({ x: tx(daily[best].day), y: y(Number(daily[best].amount)) - 10, title: dayLabel(daily[best].day),
      lines: [moneyIn(daily[best].amount, currency), ...(spikeDays.has(daily[best].day) ? ["pico respecto de los días anteriores"] : [])] });
  };

  return (
    <figure className="sp-fig" ref={ref as React.RefObject<HTMLElement & HTMLDivElement>} onMouseLeave={() => { setTip(null); setHover(null); }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img"
           aria-label={`Gasto diario de ${dayLabel(daily[0].day)} a ${dayLabel(daily[daily.length - 1].day)}; ${spikes.length} picos marcados.`}>
        <defs>
          <pattern id={`${uid}-h`} width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="7" stroke="var(--pen)" strokeWidth="1.5" opacity=".22" />
          </pattern>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line className="sp-grid" x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} />
            <text className="sp-axis" x={M.l - 8} y={y(t) + 4} textAnchor="end">{compact(t)}</text>
          </g>
        ))}
        {open.map((p) => {
          const from = daily.find((d) => d.day.startsWith(p));
          return from ? <rect key={p} x={tx(from.day)} y={M.t} width={W - M.r - tx(from.day)} height={H - M.t - M.b} fill={`url(#${uid}-h)`} /> : null;
        })}
        <path className="sp-area" d={area} />
        <path className="sp-line" d={line} />
        {spikes.map((s) => (
          <g key={`${s.name}-${s.day}`}>
            <circle className="sp-spike" cx={tx(s.day)} cy={y(Number((daily.find((d) => d.day === s.day) ?? { amount: s.amount }).amount))} r={5.5} />
            <text className="sp-spike-l" x={tx(s.day)} y={y(Number((daily.find((d) => d.day === s.day) ?? { amount: s.amount }).amount)) - 12} textAnchor="middle">{dayLabel(s.day)}</text>
          </g>
        ))}
        {hover !== null && (
          <g>
            <line className="sp-cross" x1={tx(daily[hover].day)} x2={tx(daily[hover].day)} y1={M.t} y2={H - M.b} />
            <circle className="sp-dot" cx={tx(daily[hover].day)} cy={y(Number(daily[hover].amount))} r={4} />
          </g>
        )}
        {xs.map((d) => <text key={d.day} className="sp-axis" x={tx(d.day)} y={H - 10} textAnchor="middle">{dayLabel(d.day)}</text>)}
        <rect x={M.l} y={M.t} width={W - M.l - M.r} height={H - M.t - M.b} fill="transparent" onMouseMove={sx} />
      </svg>
      <Tooltip tip={tip} width={W} />
      {open.length > 0 && <figcaption className="sp-cap"><i className="sp-sw sp-sw-open" />La zona rayada es un mes incompleto en el archivo.</figcaption>}
    </figure>
  );
}

// ------------------------------------------------------------------------------------------------ qué cambió
export type Mover = { name: string; previous: string; current: string; delta: string; pct: string | null };

export function MoversBars({ movers, currency }: { movers: Mover[]; currency: string }) {
  const rows = movers.slice(0, 8);
  const reach = Math.max(1, ...rows.map((m) => Math.abs(Number(m.delta))));
  return (
    <ul className="sp-movers" aria-label="Variación de gasto por servicio entre los dos últimos meses completos">
      {rows.map((m) => {
        const d = Number(m.delta), w = (Math.abs(d) / reach) * 100;
        return (
          <li key={m.name}>
            <span className="sp-m-name" title={m.name}>{m.name}</span>
            <span className="sp-m-track" aria-hidden="true">
              <span className="sp-m-zero" />
              <span className={d >= 0 ? "sp-m-bar sp-up" : "sp-m-bar sp-down"} style={d >= 0 ? { left: "50%", width: `${w / 2}%` } : { right: "50%", width: `${w / 2}%` }} />
            </span>
            <span className={d >= 0 ? "sp-m-val sp-up-t" : "sp-m-val sp-down-t"}>
              {d >= 0 ? "+" : "−"}{moneyIn(Math.abs(d), currency)}
              <small>{m.pct === null ? "nuevo" : `${d >= 0 ? "+" : "−"}${Math.abs(Number(m.pct) * 100).toFixed(1).replace(".", ",")} %`}</small>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

// ------------------------------------------------------------------------------------------------ reparto
export type Part = { name: string; total: string; share: string; months?: Record<string, string> };

const SHADES = [1, 0.78, 0.58, 0.42, 0.3, 0.2];

export function ShareBar({ parts, currency }: { parts: Part[]; currency: string }) {
  const top = parts.filter((p) => !p.name.startsWith("Otros (")).slice(0, 5);
  const rest = parts.filter((p) => !top.includes(p));
  const restShare = rest.reduce((a, p) => a + Number(p.share), 0), restTotal = rest.reduce((a, p) => a + Number(p.total), 0);
  const segs = [...top.map((p, i) => ({ name: p.name, share: Number(p.share), total: p.total, shade: SHADES[i] })),
                ...(restShare > 0.0005 ? [{ name: `Otros (${rest.length})`, share: restShare, total: String(restTotal), shade: 0 }] : [])];
  return (
    <div className="sp-share">
      <div className="sp-share-bar" role="img" aria-label={`Reparto del gasto: ${segs.map((s) => `${s.name} ${(s.share * 100).toFixed(1)} %`).join(", ")}`}>
        {segs.map((s) => (
          <span key={s.name} className={s.shade ? "" : "sp-seg-rest"} title={`${s.name}: ${(s.share * 100).toFixed(1)} %`}
                style={{ flexGrow: Math.max(s.share, 0.004), ...(s.shade ? { background: `color-mix(in srgb, var(--pen) ${s.shade * 100}%, var(--sheet))` } : {}) }}>
            {s.share >= 0.09 ? `${(s.share * 100).toFixed(0)} %` : ""}
          </span>
        ))}
      </div>
      <ul className="sp-share-leg">
        {segs.map((s) => (
          <li key={s.name}>
            <i style={s.shade ? { background: `color-mix(in srgb, var(--pen) ${s.shade * 100}%, var(--sheet))` } : undefined} className={s.shade ? "" : "sp-seg-rest"} />
            <span>{s.name}</span><b>{(s.share * 100).toFixed(1).replace(".", ",")} %</b><em>{moneyIn(s.total, currency)}</em>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return <span className="muted">—</span>;
  const w = 74, h = 22, lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * (w - 4) + 2, h - 3 - ((v - lo) / span) * (h - 6)]);
  const up = values[values.length - 1] > values[0];
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true" className="sp-spark">
      <path d={pts.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ")} className={up ? "sp-spark-up" : "sp-spark-dn"} />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r={2.4} className={up ? "sp-spark-up-d" : "sp-spark-dn-d"} />
    </svg>
  );
}
