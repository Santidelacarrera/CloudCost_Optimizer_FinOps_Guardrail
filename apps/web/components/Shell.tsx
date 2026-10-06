"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import BrandMark from "@/components/BrandMark";
import Icon from "@/components/Icon";
import { ApiError, authApi, expireSession } from "@/lib/api";
import { ROLE_LABEL, type Me } from "@/lib/roles";

const NAV = [
  { href: "/", label: "Panel", icon: "panel" },
  { href: "/recommendations", label: "Recomendaciones", icon: "list" },
  { href: "/import", label: "Importar CSV", icon: "upload" },
  { href: "/expenses", label: "Analizar gastos", icon: "receipt" },
  { href: "/audit", label: "Auditoría", icon: "shield" },
];

const initials = (name: string) => name.split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]?.toUpperCase()).join("") || "?";

export default function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const [me, setMe] = useState<Me | null>(null);

  const load = useCallback(() => {
    authApi<Me>("me").then(setMe).catch((e) => { if (e instanceof ApiError && e.status === 401) expireSession(); });
  }, []);
  useEffect(() => {
    load();
    window.addEventListener("cc:me-changed", load);       // lo emiten las pantallas de cuenta al activar el 2FA, etc.
    return () => window.removeEventListener("cc:me-changed", load);
  }, [load]);

  const logout = async () => {
    await authApi("logout", { method: "POST" }).catch(() => null);
    window.location.href = "/login";
  };
  const items = me?.role === "ADMIN" && !me.mode ? [...NAV, { href: "/team", label: "Equipo", icon: "users" }] : NAV;
  const active = (href: string) => (href === "/" ? path === "/" : path === href || path.startsWith(`${href}/`));

  return (
    <div className="shell">
      <a className="skip" href="#contenido">Saltar al contenido</a>
      <aside className="rail">
        <Link className="brand" href="/" aria-label="CloudCost, ir al panel"><BrandMark /><span>CloudCost</span></Link>
        <nav aria-label="Principal">
          {items.map((i) => (
            <Link key={i.href} href={i.href} aria-current={active(i.href) ? "page" : undefined}><Icon name={i.icon} />{i.label}</Link>
          ))}
        </nav>
        <div className="rail-foot">
          {me ? (
            <>
              <div className="who">
                <span className="avatar" aria-hidden="true">{initials(me.full_name)}</span>
                <div><b>{me.full_name}</b><small>{me.organization}</small><small>{ROLE_LABEL[me.role] ?? me.role}</small></div>
              </div>
              {me.mfa_recommended && <Link className="nudge" href="/account#mfa">Protege tu cuenta con verificación en dos pasos</Link>}
              {!me.mode && <Link href="/account">Cuenta y seguridad</Link>}
              <button className="linklike" onClick={logout}>Cerrar sesión</button>
            </>
          ) : <span className="muted">Cargando…</span>}
        </div>
      </aside>
      <main className="content" id="contenido" tabIndex={-1}>{children}</main>
    </div>
  );
}
