export const ROLE_LABEL: Record<string, string> = {
  ADMIN: "Administración", FINOPS: "FinOps", SRE: "SRE", DEVELOPER: "Desarrollo", AUDITOR: "Auditoría", VIEWER: "Solo lectura",
};
export const ROLE_HELP: Record<string, string> = {
  ADMIN: "Gestiona el equipo, aprueba y ve la auditoría",
  FINOPS: "Escanea, aprueba y verifica ahorros",
  SRE: "Escanea, aprueba y marca despliegues",
  DEVELOPER: "Consulta recomendaciones y pull requests",
  AUDITOR: "Lee la auditoría inmutable",
  VIEWER: "Solo consulta",
};
export const ROLES = ["ADMIN", "FINOPS", "SRE", "DEVELOPER", "AUDITOR", "VIEWER"] as const;

export type Me = {
  mode?: "dev"; id?: string; email: string; full_name: string; role: string; organization: string; mfa_enabled: boolean;
  mfa_recommended: boolean; recovery_codes_left?: number; email_verified?: boolean; password_changed_at?: string | null;
  last_login_at?: string | null; created_at?: string; sso?: boolean;
};
