export type Health = {
  status: string
  service: string
  components: {
    economic_wallet: string
    micro_billing: string
    memory: { status: string; active_memories: number; ledger_verified: boolean }
    pocket_os: { status: string; event_ledger_verified: boolean }
    external_model_executor: string
  }
}

export type Project = { project_id: string; name: string; description: string; status: string; created_at: string }
export type Loop = { loop_id: string; project_id: string; title: string; objective: string; status: string; created_at: string }
export type Shadow = { shadow_id: string; project_id: string; kind: string; content: string; confidence: number; evidence: string; authority: 'NONE'; can_execute: false; can_ratify: false; created_at: string }
export type Proposal = {
  proposal_id: string; project_id: string; loop_id?: string; shadow_id?: string; agent_id: string; title: string; category: string; candidate: string; proposed_cost: string; recurring: number; access_level: string; expected_benefit: string; evidence: string; recommendation: string; status: string; authority: string; execution_authorized: false; created_at: string; approved_at?: string
}
export type Wallet = { id: string; total: string; available: string; reserved: string; spent: string; minimum_liquidity: string; development_reserve: string; safety_reserve: string; owner_reserve: string; development_rate_bps: number; safety_rate_bps: number; owner_rate_bps: number; created_at: string }
export type UpgradeProposal = { proposal_id: string; wallet_id: string; target_module: string; estimated_cost: string; predicted_monthly_value: string; predicted_roi: number; actual_roi: number | null; status: string; rdp_metrics: string | null; scenario_paths: string; selected_path: string | null; signature_proof: string | null; approval_actor: string | null; reserved_amount: string; evidence: string; created_at: string; updated_at: string }
export type LedgerEvent = { id: string; wallet_id: string; mission_id?: string; reservation_id?: string; event: string; amount: string; metadata: string; created_at: string }
export type MemoryHealth = { status: string; active_memories: number; ledger_verified: boolean }
export type HFModel = { model_id: string; display_name: string; capabilities: string[]; context_window: number; quality_score: number; reliability: number; latency_ms: number; input_usd_per_1m: string | null; output_usd_per_1m: string | null; providers: string[]; status: string; pricing_source: string; pricing_verified: boolean }
export type HFProviderStatus = { provider: string; endpoint: string; token_configured: boolean; verified_priced_models: number; catalog_size: number; execution_status: string; secrets_are_server_side: boolean }
export type HFQuote = { status: string; selected_model: HFModel; routing_policy: string; estimated_cost: string; input_tokens: number; output_tokens: number; alternatives: { model_id: string; estimated_cost: string; quality_score: number; latency_ms: number }[]; execution_authorized: false; wallet_reservation_required: true; note: string }

const defaultBase = 'https://8000-inn0u6a8zgt70vdzkc85q-e5a55957.us1.manus.computer'
export const getApiBase = () => localStorage.getItem('pocket_api_base') || import.meta.env.VITE_API_BASE_URL || defaultBase
export const setApiBase = (value: string) => localStorage.setItem('pocket_api_base', value.replace(/\/$/, ''))

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBase()}${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) } })
  if (!response.ok) {
    const body = await response.text()
    throw new Error(body || `${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<Health>('/health'),
  projects: () => request<Project[]>('/pocket/projects'),
  createProject: (body: { name: string; description: string }) => request<Project>('/pocket/projects', { method: 'POST', body: JSON.stringify(body) }),
  loops: (projectId: string) => request<Loop[]>(`/pocket/projects/${projectId}/loops`),
  createLoop: (projectId: string, body: { title: string; objective: string }) => request<Loop>(`/pocket/projects/${projectId}/loops`, { method: 'POST', body: JSON.stringify(body) }),
  shadow: (projectId?: string) => request<Shadow[]>(`/pocket/shadow${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`),
  createShadow: (projectId: string, body: { kind: string; content: string; confidence: number; evidence: Record<string, unknown>[] }) => request<Shadow>(`/pocket/projects/${projectId}/shadow`, { method: 'POST', body: JSON.stringify(body) }),
  proposals: (status?: string) => request<Proposal[]>(`/pocket/proposals${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  decideProposal: (proposalId: string, decision: 'approve' | 'reject' | 'request_more_testing', actor: string) => request<Proposal & { note?: string }>(`/pocket/proposals/${proposalId}/decision`, { method: 'POST', body: JSON.stringify({ decision, actor }) }),
  wallets: (id: string) => request<Wallet>(`/wallets/${id}`),
  walletDeposit: (id: string, amount: string) => request<Wallet>(`/wallets/${id}/deposit`, { method: 'POST', body: JSON.stringify({ amount }) }),
  upgradeProposals: (walletId: string) => request<UpgradeProposal[]>(`/upgrades/proposals?wallet_id=${encodeURIComponent(walletId)}`),
  ledger: (walletId: string) => request<LedgerEvent[]>(`/ledger/${walletId}`),
  memoryHealth: () => request<MemoryHealth>('/memory/health'),
  searchMemory: (query: string) => request<Record<string, unknown>[]>('/memory/search', { method: 'POST', body: JSON.stringify({ query, top_k: 10 }) }),
  events: () => request<Record<string, unknown>[]>('/pocket/events'),
  hfModels: (capability?: string) => request<{ source: string; routing_policies: string[]; models: HFModel[]; note: string }>(`/hf/models${capability ? `?capability=${encodeURIComponent(capability)}` : ''}`),
  hfProviderStatus: () => request<HFProviderStatus>('/hf/provider-status'),
  hfQuote: (body: { capabilities: string[]; input_tokens: number; output_tokens: number; max_cost: string; minimum_quality?: number; routing_policy?: string; preferred_providers?: string[]; model_id?: string }) => request<HFQuote>('/hf/quote', { method: 'POST', body: JSON.stringify(body) }),
}
