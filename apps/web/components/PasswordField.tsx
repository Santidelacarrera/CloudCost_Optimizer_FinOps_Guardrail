"use client";
import { useId, useState } from "react";

type Rule = { ok: boolean; text: string };

/** Réplica orientativa de la política del servidor (la API es la que decide). */
export function checkPassword(pw: string, personal: string[] = []): { score: 0 | 1 | 2 | 3 | 4; rules: Rule[] } {
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(pw)).length;
  const low = pw.toLowerCase();
  const seq = ["qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890", "abcdefghijklmnopqrstuvwxyz"]
    .some((row) => [row, [...row].reverse().join("")].some((s) => { for (let i = 0; i + 5 <= s.length; i++) if (low.includes(s.slice(i, i + 5))) return true; return false; }));
  const tokens = personal.flatMap((p) => p.toLowerCase().split(/[^a-z0-9]+/)).filter((t) => t.length >= 4);
  const rules: Rule[] = [
    { ok: pw.length >= 12, text: "Al menos 12 caracteres" },
    { ok: classes >= 3 || pw.length >= 20, text: "Mezcla 3 tipos (minúsculas, mayúsculas, números, símbolos) o usa 20+ caracteres" },
    { ok: pw.length > 0 && !seq && !/(.)\1{3,}/.test(pw), text: "Sin secuencias ni repeticiones (12345, qwerty, aaaa)" },
    { ok: pw.length > 0 && !tokens.some((t) => low.includes(t)), text: "Sin tu nombre ni tu correo" },
  ];
  const ok = rules.filter((r) => r.ok).length;
  const score = !pw ? 0 : ok === 4 ? (pw.length >= 16 ? 4 : 3) : ok >= 3 ? 2 : 1;
  return { score: score as 0 | 1 | 2 | 3 | 4, rules };
}
const LABEL = ["", "Débil", "Mejorable", "Buena", "Excelente"];

type Props = {
  label: string; value: string; onChange: (v: string) => void; autoComplete: "new-password" | "current-password";
  meter?: boolean; personal?: string[]; hint?: string; invalid?: boolean; name?: string; required?: boolean;
};

export default function PasswordField({ label, value, onChange, autoComplete, meter, personal, hint, invalid, name, required = true }: Props) {
  const id = useId();
  const [show, setShow] = useState(false);
  const c = meter ? checkPassword(value, personal) : null;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="pw">
        <input id={id} name={name} type={show ? "text" : "password"} value={value} onChange={(e) => onChange(e.target.value)} autoComplete={autoComplete}
               required={required} maxLength={256} aria-invalid={invalid || undefined} aria-describedby={c ? `${id}-r` : hint ? `${id}-h` : undefined}
               autoCapitalize="none" spellCheck={false} />
        <button type="button" className="quiet" onClick={() => setShow((s) => !s)} aria-pressed={show}>{show ? "Ocultar" : "Mostrar"}</button>
      </div>
      {hint && !c && <span className="hint" id={`${id}-h`}>{hint}</span>}
      {c && (
        <div id={`${id}-r`}>
          <div className="meter" data-s={c.score} role="img" aria-label={`Seguridad de la contraseña: ${LABEL[c.score] || "sin escribir"}`}><i /><i /><i /><i /></div>
          {c.score > 0 && <div className="meter-label" aria-live="polite">{LABEL[c.score]}</div>}
          <ul className="rules">{c.rules.map((r) => <li key={r.text} className={r.ok ? "ok" : ""}>{r.text}</li>)}</ul>
        </div>
      )}
    </div>
  );
}
