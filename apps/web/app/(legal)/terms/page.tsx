import type { Metadata } from "next";

export const metadata: Metadata = { title: "Términos de uso · CloudCost" };

// BORRADOR: lo redacta el equipo técnico a partir de lo que el sistema hace. Un abogado debe revisarlo y completar los [CORCHETES] antes del lanzamiento.
export default function Terms() {
  return (
    <article>
      <p className="note warn" role="note"><b>Borrador sin revisión legal.</b> Reemplaza los datos entre [corchetes] y haz que un abogado lo revise antes de aceptar clientes.</p>
      <h1>Términos de uso</h1>
      <p className="muted">Última actualización: [FECHA]</p>

      <h2>1. Quiénes somos</h2>
      <p>CloudCost Optimizer (&ldquo;CloudCost&rdquo;) es un servicio de [RAZÓN SOCIAL], RUT [RUT], con domicilio en [DIRECCIÓN]. Para consultas: [CORREO DE CONTACTO].</p>

      <h2>2. Qué hace el servicio</h2>
      <p>CloudCost analiza la información de infraestructura y de gasto que tú conectas o subes, detecta posibles desperdicios, explica cada hallazgo y propone cambios mediante Pull Requests. <b>CloudCost no modifica tu infraestructura en producción</b>: toda propuesta requiere la aprobación de una persona de tu organización.</p>

      <h2>3. Las recomendaciones son orientativas</h2>
      <p>Los hallazgos y los ahorros estimados son pistas para revisar, no garantías. Tú decides qué aprobar y eres responsable de validar cada cambio antes de desplegarlo. CloudCost no responde por pérdidas derivadas de cambios que apruebes o apliques.</p>

      <h2>4. Tu cuenta</h2>
      <p>Debes entregar datos verdaderos, mantener en secreto tu contraseña y tus códigos de recuperación, y avisarnos si sospechas un acceso no autorizado. Eres responsable de la actividad realizada con tu cuenta. Recomendamos activar la verificación en dos pasos, sobre todo si puedes aprobar cambios.</p>

      <h2>5. Uso aceptable</h2>
      <p>No puedes usar el servicio para vulnerar la ley, acceder a cuentas o sistemas sin autorización, interferir con su funcionamiento, ni subir información que no tengas derecho a tratar. Las credenciales de nube que conectes deben ser de solo lectura y de tu propiedad o autorizadas por su titular.</p>

      <h2>6. Tus datos</h2>
      <p>Tú conservas la titularidad de los datos que conectas o subes. Los tratamos solo para prestarte el servicio, según la <a href="/privacy">Política de privacidad</a>.</p>

      <h2>7. Disponibilidad</h2>
      <p>Procuramos mantener el servicio disponible, pero puede haber interrupciones por mantenimiento o causas ajenas. [COMPLETAR: nivel de servicio comprometido, si lo hay].</p>

      <h2>8. Planes y pagos</h2>
      <p>[COMPLETAR: precios, facturación, renovación y reembolsos, o indicar que el servicio es gratuito durante la etapa inicial].</p>

      <h2>9. Responsabilidad</h2>
      <p>En la medida que la ley lo permita, [COMPLETAR: límite de responsabilidad]. Nada de esto limita derechos irrenunciables del consumidor.</p>

      <h2>10. Terminación</h2>
      <p>Puedes dejar de usar el servicio cuando quieras y pedir la eliminación de tu cuenta. Podemos suspender cuentas que incumplan estos términos o pongan en riesgo la seguridad del servicio.</p>

      <h2>11. Cambios y ley aplicable</h2>
      <p>Si cambiamos estos términos de forma relevante, te avisaremos por correo con anticipación razonable. Se rigen por las leyes de [PAÍS] y los tribunales competentes serán los de [CIUDAD].</p>
    </article>
  );
}
