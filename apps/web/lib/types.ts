export type Rec = {
  id: string; rule_id: string; action: string; status: string; title: string; summary: string;
  current_monthly_cost: string | number; projected_monthly_cost: string | number; estimated_monthly_savings: string | number;
  confidence: string | number; risk: "LOW" | "MEDIUM" | "HIGH"; impact: string; priority: string;
  destructive: boolean; automation_blocked: boolean; approvals_required: number; version: number;
  resource_id: string; resource_name: string | null; service: string; environment: string | null; region: string | null;
  created_at: string; updated_at: string;
};
export type RecDetail = Rec & {
  explanation: string | null; alternatives: { title?: string; description?: string }[] | string[]; evidence: Record<string, unknown>;
  llm_advice: Record<string, unknown> | null; instance_type: string | null; iac_address: string | null; iac_file: string | null;
  approvals: { id: string; decision: string; reason: string; approver_role: string; recommendation_version: number; email: string; created_at: string }[];
  timeline: { from_status: string | null; to_status: string; actor_type: string; actor_id: string | null; note: string | null; created_at: string }[];
  pull_request: { number: number; url: string; branch: string; draft: boolean; state: string; diff: string | null; validations: unknown } | null;
  savings_verification: Record<string, unknown> | null;
};
export type Summary = {
  monthly_spend: number; potential_savings: number; savings_pct: number; recommendations: number; pending_approval: number;
  high_risk: number; verified: number; rejected: number; realized_savings: number; realization_pct: number | null;
  by_status: Record<string, number>; top_recommendations: Rec[];
};
export type AuditEvent = {
  seq: number; event_type: string; actor_type: string; actor_id: string | null; entity_type: string | null;
  entity_id: string | null; payload: Record<string, unknown>; hash: string; created_at: string;
};
