"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

export default function Nav() {
  const path = usePathname();
  const router = useRouter();
  if (path.startsWith("/login")) return null;
  const logout = async () => { await fetch("/api/session", { method: "DELETE" }); router.push("/login"); };
  return (
    <header className="nav">
      <b>☁ CloudCost Optimizer</b>
      <Link href="/">Panel</Link>
      <Link href="/recommendations">Recomendaciones</Link>
      <Link href="/audit">Auditoría</Link>
      <span className="sp" />
      <button className="secondary" onClick={logout}>Salir</button>
    </header>
  );
}
