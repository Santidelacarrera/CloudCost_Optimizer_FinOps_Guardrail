import Link from "next/link";
import type { ReactNode } from "react";
import BrandMark from "@/components/BrandMark";

export default function LegalLayout({ children }: { children: ReactNode }) {
  return (
    <div className="legal">
      <header>
        <Link className="brand" href="/" aria-label="CloudCost, ir al inicio"><BrandMark /><span>CloudCost</span></Link>
        <nav aria-label="Documentos legales">
          <Link href="/terms">Términos de uso</Link>
          <Link href="/privacy">Política de privacidad</Link>
          <Link href="/login">Iniciar sesión</Link>
        </nav>
      </header>
      <main id="contenido">{children}</main>
    </div>
  );
}
