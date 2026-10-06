import Link from "next/link";
import type { ReactNode } from "react";
import BrandMark from "@/components/BrandMark";

// Marco de las pantallas de acceso: a la izquierda una hoja de libro mayor donde el resaltador marca lo que sobra; a la derecha el formulario.
export default function AuthFrame({ children }: { children: ReactNode }) {
  return (
    <div className="auth">
      <section className="auth-art" aria-hidden="true">
        <div className="brand" style={{ padding: 0 }}><BrandMark size={34} /><span>CloudCost</span></div>
        <div>
          <h2><span>Encuentra lo que sobra.</span><span>Aprueba lo que cambia.</span></h2>
          <p className="lede">CloudCost revisa tu infraestructura y tus gastos, explica cada hallazgo y propone el cambio en un Pull Request. Nada se aplica sin que una persona lo apruebe.</p>
        </div>
        <div>
          <div className="ledger-demo">
            <div className="li"><span>Servidor web-prod-1 usa el 9 % de su CPU</span><b>USD 241,20</b></div>
            <div className="li"><span>Disco sin adjuntar hace 45 días</span><b>USD 38,40</b></div>
            <div className="li"><span>Copias de seguridad de hace 200 días</span><b>USD 21,30</b></div>
            <div className="li sum"><span>Ahorro mensual propuesto</span><b><span className="sweep">USD 300,90</span></b></div>
          </div>
          <p className="fine">Ejemplo con datos ficticios.</p>
        </div>
      </section>
      <section className="auth-pane">
        <div className="brand auth-brand-m" style={{ padding: 0 }}><BrandMark /><span>CloudCost</span></div>
        <div className="auth-card">{children}</div>
        <p className="auth-foot">Contraseñas protegidas con scrypt y verificación en dos pasos opcional. Tus archivos CSV se analizan en memoria y no se guardan.</p>
        <p className="auth-foot">Al usar CloudCost aceptas los <Link href="/terms">Términos de uso</Link> y la <Link href="/privacy">Política de privacidad</Link>.</p>
      </section>
    </div>
  );
}
