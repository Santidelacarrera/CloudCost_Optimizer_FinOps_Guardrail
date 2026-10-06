"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import PageHead from "@/components/PageHead";

type Repo = { id: string; full_name: string; provider: string };

export default function ImportPage() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [repo, setRepo] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => { api<Repo[]>("repositories").then(setRepos).catch(() => {}); }, []);

  const template = async () => {
    const r = await fetch("/api/proxy/imports/template");
    const blob = new Blob([await r.text()], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "plantilla_cloudcost.csv"; a.click();
    URL.revokeObjectURL(a.href);
  };

  const submit = async () => {
    if (!file) return;
    setBusy(true); setMsg(""); setOk(false);
    try {
      const csv_text = await file.text();
      const r = await api<{ rows: number }>("imports", { method: "POST", body: { filename: file.name, csv_text, repository_id: repo || null } });
      setOk(true); setMsg(`Archivo válido: ${r.rows} filas. El análisis está en curso; mira el resultado en el Panel y en Recomendaciones.`);
    } catch (e: any) { setMsg(e.message); } finally { setBusy(false); }
  };

  return (
    <>
      <PageHead title="Importar CSV" sub="Una fila por recurso, con su uso y su costo. Se aplican las mismas reglas, políticas y aprobaciones que en un escaneo." />
      <div className="card">
        <p>Sube un CSV con <b>una fila por recurso</b> (instancias EC2, volúmenes EBS o snapshots) incluyendo uso (CPU/memoria) y costo.
          Se aplican las mismas reglas, políticas y aprobaciones que en un escaneo normal. Si tu archivo es un Excel, usa <i>Guardar como → CSV UTF-8</i>.</p>
        <p className="muted">Columnas obligatorias: <code>resource_id</code>, <code>service</code> (ec2 | ebs | ebs_snapshot). Para detectar sobredimensionamiento hacen falta <code>cpu_avg</code>, <code>memory_avg</code> y <code>observation_days</code> (≥14).
          Un presupuesto genérico sin recursos ni uso no permite detectar desperdicio.</p>
        <div className="row"><button className="secondary" onClick={template}>Descargar plantilla</button></div>
        <h2>Subir archivo</h2>
        <div className="grid" style={{ gridTemplateColumns: "1fr", maxWidth: 520 }}>
          <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="Archivo CSV" />
          <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label="Repositorio IaC">
            <option value="">Sin repositorio IaC (solo recomendaciones, sin parche)</option>
            {repos.map((r) => <option key={r.id} value={r.id}>{r.full_name} ({r.provider})</option>)}
          </select>
          <button onClick={submit} disabled={!file || busy}>{busy ? "Analizando…" : "Analizar archivo"}</button>
        </div>
        {msg && <p className={`note ${ok ? "ok" : "bad"}`} role={ok ? "status" : "alert"} style={{ marginTop: 14 }}>{msg}</p>}
        {ok && <p><Link href="/recommendations">Ver recomendaciones</Link></p>}
        <p className="muted">Para generar el Pull Request, los recursos deben poder vincularse con tu Terraform (por ID o por la etiqueta Name).</p>
      </div>
    </>
  );
}
