import type { Metadata } from "next";

export const metadata: Metadata = { title: "Política de privacidad · CloudCost" };

// BORRADOR: describe lo que el sistema realmente hace hoy (ver docs/security.md). Un abogado debe revisarlo y completar los [CORCHETES].
export default function Privacy() {
  return (
    <article>
      <p className="note warn" role="note"><b>Borrador sin revisión legal.</b> Reemplaza los datos entre [corchetes], confirma los puntos marcados [REVISAR] y haz que un abogado lo revise antes de aceptar clientes.</p>
      <h1>Política de privacidad</h1>
      <p className="muted">Última actualización: [FECHA]</p>

      <h2>1. Responsable</h2>
      <p>[RAZÓN SOCIAL], RUT [RUT], [DIRECCIÓN]. Contacto de privacidad: [CORREO DE PRIVACIDAD].</p>

      <h2>2. Qué datos tratamos</h2>
      <ul>
        <li><b>Cuenta:</b> nombre, correo, nombre de tu organización y rol. La contraseña <b>no se guarda</b>: solo un hash irreversible (scrypt) y, si activas la verificación en dos pasos, el secreto cifrado y los códigos de recuperación también como hash.</li>
        <li><b>Seguridad de acceso:</b> dirección IP y navegador de tus sesiones, intentos de inicio de sesión (el correo se guarda como huella, no en claro) y un registro de auditoría de acciones relevantes.</li>
        <li><b>Datos de nube:</b> inventario de recursos, métricas de uso y costos que CloudCost lee con el rol de solo lectura que tú creas.</li>
        <li><b>Archivos que subes:</b> el analizador de gastos procesa el archivo sin guardarlo y registra solo cifras agregadas. La importación de costos conserva los registros de costo importados. [REVISAR]</li>
      </ul>

      <h2>3. Para qué los usamos</h2>
      <p>Para crear y proteger tu cuenta, prestarte el servicio, detectar y explicar desperdicios, mantener un registro de auditoría y enviarte correos transaccionales (confirmación de cuenta, recuperación de contraseña, avisos de seguridad e invitaciones). No vendemos tus datos ni los usamos para publicidad.</p>

      <h2>4. Con quién los compartimos</h2>
      <p>Solo con proveedores que hacen posible el servicio: alojamiento e infraestructura [PROVEEDOR], envío de correo [PROVEEDOR] y, si lo activas, GitHub (para crear Pull Requests) y un proveedor de IA ([PROVEEDOR], desactivado por defecto; no recibe credenciales). Si eliges una contraseña, solo se envía a un servicio de contraseñas filtradas el prefijo parcial de su huella, nunca la contraseña. [REVISAR]</p>

      <h2>5. Cuánto tiempo los conservamos</h2>
      <p>Los datos de tu cuenta, mientras la tengas. Las sesiones vencen a las 12 horas o tras 2 horas de inactividad. El registro de auditoría es de solo escritura por diseño y se conserva [PLAZO]. Las copias de seguridad se conservan [PLAZO]. Al eliminar tu cuenta borramos tus datos salvo lo que la ley nos obligue a guardar.</p>

      <h2>6. Seguridad</h2>
      <p>Cifrado en tránsito (HTTPS), aislamiento de datos por organización a nivel de base de datos, contraseñas con scrypt, verificación en dos pasos opcional, sesiones revocables y límites contra intentos de acceso masivos. Ningún sistema es infalible: si ocurre un incidente que te afecte, te avisaremos.</p>

      <h2>7. Tus derechos</h2>
      <p>Puedes pedir acceso, rectificación, eliminación, oposición y portabilidad de tus datos según la normativa de protección de datos personales aplicable (en Chile, la Ley N.º 19.628 y la Ley N.º 21.719 cuando entre en vigor). Escríbenos a [CORREO DE PRIVACIDAD]. [REVISAR: procedimiento y plazos de respuesta]</p>

      <h2>8. Cookies</h2>
      <p>Usamos solo cookies estrictamente necesarias para mantener tu sesión (httpOnly, sin acceso desde JavaScript y sin rastreo). No usamos cookies de publicidad ni de analítica de terceros.</p>

      <h2>9. Cambios</h2>
      <p>Si cambiamos esta política de forma relevante, te avisaremos por correo.</p>
    </article>
  );
}
