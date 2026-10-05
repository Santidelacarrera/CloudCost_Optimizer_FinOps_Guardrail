"use client";
import { use, useCallback, useEffect, useState } from "react";
import { api, fmtDate, pct, usd } from "@/lib/api";
import type { RecDetail } from "@/lib/types";
import Badge from "@/components/Badge";

function Diff({ text }: { text: string }) {
  return (
    <pre className="diff">
      {text.split("\n").map((l, i) => (
        <div key={i} className={l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : ""}>{l || " "}</div>
      ))}
    </pre>
  );
}

export default function Detail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [r, setR] = useState<RecDetail | null>(null);
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => api<RecDetail>(`recommendations/${id}`).then(setR).catch((e) => setMsg(e.message)), [id]);
  useEffect(() => { load(); }, [load]);

  const act = async (path: string, body?: unknown) => {
    setBusy(true); setMsg("");
    try { await api(`recommendations/${id}/${path}`, { method: "POST", body: body ?? {} }); setReason(""); await load(); }
    catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  };
  if (!r) return <p className={msg ? "err" : "muted"}>{msg || "Cargando…"}</p>;

  const decide = (d: "approve" | "reject") => act(d, { reason, expected_version: r.version });
  const canDecide = r.status === "PENDING_APPROVAL";
  const evidence = Object.entries(r.evidence).filter(([, v]) => typeof v !== "object");
  const advice = r.llm_advice as { justification?: string; confidence?: number; risk?: string; alternatives?: { action: string; description: string }[] } | null;

  return (
    <>
      <h1>{r.title}</h1>
      <div className="row"><Badge value={r.status} kind="" /><Badge value={r.risk} /><Badge value={r.priority} kind="" />
        {r.destructive && <Badge value="destructiva" kind="warn" />}
        {r.automation_blocked && <Badge value="automatización bloqueada" kind="bad" />}</div>
      <p>{r.summary}</p>
      <div className="grid">
        <div className="card kpi"><div className="v">{usd(r.estimated_monthly_savings)}</div><div className="l">Ahorro estimado / mes</div></div>
        <div className="card kpi"><div className="v">{usd(r.current_monthly_cost)} → {usd(r.projected_monthly_cost)}</div><div className="l">Coste actual → proyectado</div></div>
        <div className="card kpi"><div className="v">{pct(r.confidence)}</div><div className="l">Confianza</div></div>
        <div className="card kpi"><div className="v">{r.approvals.filter((a) => a.decision === "APPROVED" && a.recommendation_version === r.version).length} / {r.approvals_required}</div><div className="l">Aprobaciones</div></div>
      </div>

      <h2>Por qué</h2>
      <div className="card">
        {r.explanation && <p style={{ whiteSpace: "pre-wrap" }}>{r.explanation}</p>}
        <table><tbody>{evidence.map(([k, v]) => <tr key={k}><td className="muted">{k}</td><td>{String(v)}</td></tr>)}</tbody></table>
        <p className="muted">Recurso: {r.resource_id} · {r.service} · {r.environment ?? "entorno desconocido"} · {r.region}{r.iac_address ? ` · IaC: ${r.iac_address}` : ""}</p>
      </div>

      {advice && (
        <>
          <h2>Asesor LLM (informativo, no ejecuta nada)</h2>
          <div className="card"><p>{advice.justification}</p>
            <p className="muted">Riesgo según el asesor: {advice.risk} · confianza {advice.confidence != null ? pct(advice.confidence) : "—"}</p>
            {advice.alternatives?.length ? <><b>Alternativas</b><ul>{advice.alternatives.map((x, i) => <li key={i}><b>{x.action}</b>: {x.description}</li>)}</ul></> : null}</div>
        </>
      )}

      <h2>Acciones</h2>
      <div className="card">
        {canDecide && (
          <>
            <textarea rows={2} placeholder="Motivo de la decisión (obligatorio, mín. 3 caracteres)" value={reason} onChange={(e) => setReason(e.target.value)} />
            <div className="row" style={{ marginTop: 8 }}>
              <button disabled={busy || reason.trim().length < 3} onClick={() => decide("approve")}>Aprobar</button>
              <button className="danger" disabled={busy || reason.trim().length < 3} onClick={() => decide("reject")}>Rechazar</button>
              {r.approvals_required > 1 && <span className="muted">Aprobación reforzada: {r.approvals_required} aprobadores distintos, al menos uno ADMIN/SRE.</span>}
            </div>
          </>
        )}
        <div className="row" style={{ marginTop: 8 }}>
          {r.status === "APPROVED" && <button disabled={busy} onClick={() => act("create-pr")}>Crear Pull Request</button>}
          {r.status === "PR_CREATED" && <button className="secondary" disabled={busy} onClick={() => act("mark-merged")}>Simular merge (solo demo)</button>}
          {r.status === "MERGED" && <button className="secondary" disabled={busy} onClick={() => act("deployed", { reference: "ui" })}>Marcar desplegado</button>}
          {r.status === "DEPLOYED" && <button disabled={busy} onClick={() => act("verify-savings", {})}>Verificar ahorro</button>}
        </div>
        {!canDecide && !["APPROVED", "PR_CREATED", "MERGED", "DEPLOYED"].includes(r.status) && <p className="muted">Sin acciones disponibles en este estado.</p>}
        {msg && <p className="err">{msg}</p>}
        <p className="muted">La plataforma nunca fusiona ni despliega por sí sola: el merge y el despliegue ocurren en tu repositorio/CI.</p>
      </div>

      {r.pull_request && (
        <>
          <h2>Pull Request #{r.pull_request.number}{r.pull_request.draft ? " (borrador)" : ""}</h2>
          <div className="card">
            <p><a href={r.pull_request.url} target="_blank" rel="noopener noreferrer">{r.pull_request.url}</a> · rama {r.pull_request.branch} · {r.pull_request.state}</p>
            {r.pull_request.diff && <Diff text={r.pull_request.diff} />}
          </div>
        </>
      )}

      <h2>Aprobaciones</h2>
      <div className="card"><table><tbody>
        {r.approvals.map((a) => <tr key={a.id}><td>{a.email} ({a.approver_role})</td><td><Badge value={a.decision} kind={a.decision === "APPROVED" ? "ok" : "bad"} /></td><td>v{a.recommendation_version}</td><td>{a.reason}</td><td className="muted">{fmtDate(a.created_at)}</td></tr>)}
        {r.approvals.length === 0 && <tr><td className="muted">Sin decisiones todavía.</td></tr>}
      </tbody></table></div>

      <h2>Línea de tiempo</h2>
      <div className="card"><ol className="timeline">
        {r.timeline.map((t, i) => <li key={i}><b>{t.to_status}</b> <span className="muted">· {fmtDate(t.created_at)} · {t.actor_id ?? t.actor_type}{t.note ? ` · ${t.note}` : ""}</span></li>)}
      </ol></div>
    </>
  );
}
