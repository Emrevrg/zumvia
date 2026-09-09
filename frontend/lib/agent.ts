/**
 * ZUMVIA — Komuta ajanı istemcisi (Next.js)
 * Yerleşik arayüzle birebir aynı API'yi kullanır.
 */
import { api } from "./api";

export type CouncilMode = "auto" | "solo" | "council" | "strict";
export type ControlTool = "api" | "claude_code" | "codex" | "gemini_cli";

export type WorkMode = "ask" | "plan" | "agent";

export type AgentSession = {
  id: number;
  title: string;
  control_tool: ControlTool;
  control_credential_id: number | null;
  control_model: string;
  sub_credential_id: number | null;
  sub_model: string;
  council_mode: CouncilMode;
  work_mode: WorkMode;
  autonomous: boolean;
  heartbeat_seconds: number;
  status: "idle" | "thinking" | "running" | "error";
  last_error: string;
  last_active_at: string | null;
};

export type AgentMessage = {
  id?: number;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  tool_name?: string;
  tool_args?: Record<string, unknown> | null;
  tool_result?: Record<string, any> | null;
  ok?: boolean;
  duration_ms?: number;
  ts?: string;
};

export type AgentCatalog = {
  control_tools: { id: ControlTool; label: string; available: boolean; desc: string }[];
  tools: { name: string; description: string; mutating: boolean }[];
  quick_missions: { id: string; label: string; prompt: string }[];
  playbooks?: Playbook[];
  capabilities?: {
    strategies: number;
    playbooks: number;
    tools: number;
    max_risk_pct: number;
  };
};

/** Sistemin içinde hazır duran, doğrulanmış ticaret sistemi. */
export type Playbook = {
  id: string;
  label: string;
  thesis: string;
  strategies: string[];
  min_agree: number;
  timeframe: string;
  risk_pct: number;
  horizon: string;
  strength: string;
  weakness: string;
  avoid_when: string;
  tags: string[];
};

export const WORK_MODE_LABEL: Record<WorkMode, string> = {
  ask: "Sor",
  plan: "Planla",
  agent: "Uygula",
};

export const WORK_MODE_HELP: Record<WorkMode, string> = {
  ask: "Okur, ölçer, anlatır. Hiçbir şeyi değiştirmez.",
  plan: "Ne yapacağını yazar; onayın olmadan uygulamaz.",
  agent: "Kurar, çalıştırır ve 7/24 denetler.",
};

export const MODE_LABEL: Record<CouncilMode, string> = {
  auto: "Otomatik mod",
  solo: "Tek model",
  council: "Konsey",
  strict: "Sağlamcı",
};

export const MODE_HELP: Record<CouncilMode, string> = {
  auto: "Tek anahtar varsa tek model, iki+ anahtar varsa konsey çalışır.",
  solo: "En hızlı ve en ucuz. Tek model karar verir.",
  council: "Modeller paralel bakar, ağırlıklı oy + medyan seviyeler. Anlaşamazlarsa işlem yok.",
  strict: "Konsey + risk eleştirmeni + hakem + algoritma mutabakatı. En az işlem, en yüksek kalite.",
};

/** Araç adının insan okunur karşılığı — chat adım kartlarında kullanılır. */
export const TOOL_TITLE: Record<string, string> = {
  get_market_snapshot: "Piyasayı okudu",
  run_strategy_engine: "Algoritmayla doğruladı",
  scan_markets: "Piyasaları taradı",
  validate_strategy: "Walk-forward doğrulama yaptı",
  optimize_setup: "En iyi kurulumu aradı",
  get_news: "Haberlere baktı",
  get_quote: "Fiyat aldı",
  run_backtest: "Geri test yaptı",
  get_portfolio: "Portföye baktı",
  get_portfolio_risk: "Portföy riskini ölçtü",
  get_recovery_plan: "Toparlanma planını okudu",
  get_bot_detail: "Botu inceledi",
  create_bot: "Bot kurdu",
  update_bot: "Ayar değiştirdi",
  control_bot: "Botu yönetti",
  run_bot_cycle: "Bot turu çalıştırdı",
  open_position: "Pozisyon açtı",
  close_position: "Pozisyon kapattı",
  ask_sub_model: "Alt modele sordu",
  send_notification: "Bildirim gönderdi",
  system_health: "Sistemi denetledi",
  get_safety_status: "Güvenlik durumunu okudu",
  enable_live_trading: "Gerçek para moduna geçirmeyi denedi",
  disable_live_trading: "Sanal moda aldı",
  activate_kill_switch: "Acil fren çekti",
  get_model_scoreboard: "Model sicilini okudu",
  list_credentials: "Anahtarları listeledi",
  get_fundamentals: "Temel analizi okudu",
  get_earnings: "Bilanço takvimini okudu",
  compare_peers: "Emsalleri karşılaştırdı",
  get_macro_regime: "Makro rejimi okudu",
  convert_currency: "Döviz çevirdi",
  create_skill: "Beceri yazdı",
  run_skill: "Beceri çalıştırdı",
  create_automation: "Otomasyon kurdu",
  run_automation_now: "Otomasyonu çalıştırdı",
};

export const MUTATING_TOOLS = new Set([
  "create_bot", "update_bot", "control_bot", "run_bot_cycle", "open_position",
  "close_position", "send_notification", "enable_live_trading",
  "disable_live_trading", "activate_kill_switch",
  "create_skill", "edit_skill", "delete_skill", "run_skill",
  "create_automation", "edit_automation", "delete_automation", "run_automation_now",
]);

/** Araç sonucunu tek satırlık okunur özete indirger. */
export function toolSummary(message: AgentMessage): string {
  const args = (message.tool_args ?? {}) as Record<string, any>;
  const result = (message.tool_result ?? {}) as Record<string, any>;
  if (result.error) return String(result.error).slice(0, 110);

  switch (message.tool_name) {
    case "get_market_snapshot":
      return `${args.symbol ?? ""} ${args.timeframe ?? ""} · ${result.snapshot?.regime ?? ""} · ${result.snapshot?.price ?? "?"}`;
    case "run_strategy_engine":
      return `${result.action ?? ""} — ${result.summary ?? ""}`;
    case "scan_markets":
      return result.headline ?? `${result.scanned ?? 0} parite tarandı`;
    case "validate_strategy":
      return `${result.verdict ?? ""}`.slice(0, 110);
    case "optimize_setup":
      return result.headline ?? "";
    case "get_news":
      return `${result.count ?? 0} başlık · duygu ${result.sentiment_label ?? "?"}`;
    case "run_backtest":
      return `${result.metrics?.trade_count ?? 0} işlem · PF ${result.metrics?.profit_factor ?? "?"}`;
    case "get_portfolio":
      return `${result.bot_count ?? 0} bot · ${result.total_balance ?? 0} · %${result.return_pct ?? 0}`;
    case "get_portfolio_risk":
      return `ısı %${result.heat_pct ?? 0} · ${result.open_positions ?? 0} pozisyon`;
    case "get_recovery_plan":
      return result.headline ?? "";
    case "create_bot":
      return `#${result.bot_id ?? "?"} ${result.name ?? ""} · ${result.mode ?? ""}`;
    case "control_bot":
      return `${args.action ?? ""} - ${result.status ?? result.error ?? ""}`;
    case "open_position":
      return result.opened
        ? `${result.side} · giriş ${result.entry} · stop ${result.stop_loss} · R/R 1:${result.rr}`
        : `risk kalkanı reddetti — ${result.reason ?? ""}`;
    case "close_position":
      return result.closed ? `PnL ${result.pnl} (${result.r_multiple}R)` : String(result.error ?? "");
    case "enable_live_trading":
      return result.enabled ? `canlı moda alındı · sermaye ${result.capital}` : `reddedildi — ${result.reason ?? ""}`;
    case "get_safety_status":
      return `acil fren ${result.kill_switch ? "AÇIK" : "kapalı"} · canlı yetki ${result.live_authorization?.authorized ? "var" : "yok"}`;
    case "get_fundamentals":
      return result.available ? `${args.symbol} · F/K ${result.trailing_pe ?? "?"} · sektör ${result.sector ?? "?"}` : `uygulanamaz — ${result.reason ?? ""}`.slice(0,110);
    case "get_earnings":
      return result.available ? `sonraki ${result.next_earnings_date ?? "?"} · beklenen ${result.expected_eps ?? "?"}` : `uygulanamaz — ${result.reason ?? ""}`.slice(0,110);
    case "compare_peers":
      return result.available ? `medyan F/K ${result.median_peer_pe ?? "?"} · ucuz mu ${result.cheaper_than_median ?? "?"}` : `uygulanamaz — ${result.reason ?? ""}`.slice(0,110);
    case "get_macro_regime":
      return `${result.regime ?? "?"} · ${result.reasons?.[0] ?? ""}`.slice(0,110);
    case "convert_currency":
      return result.available ? `${result.amount} ${result.base} → ${result.converted} ${result.quote}` : `${result.reason ?? ""}`.slice(0,110);
    case "system_health":
      return `${result.bots_running ?? 0}/${result.bots_total ?? 0} çalışıyor`;
    default:
      return Object.entries(args).map(([k, v]) => `${k}=${v}`).join(" ").slice(0, 110);
  }
}

/* ------------------------------- API çağrıları ---------------------------- */

export const agentApi = {
  catalog: () => api<AgentCatalog>("/api/agent/catalog", { auth: false }),
  sessions: () => api<AgentSession[]>("/api/agent/sessions"),
  createSession: (body: Partial<AgentSession> & Record<string, unknown>) =>
    api<AgentSession>("/api/agent/sessions", { method: "POST", body }),
  patchSession: (id: number, body: Record<string, unknown>) =>
    api<AgentSession>(`/api/agent/sessions/${id}`, { method: "PATCH", body }),
  deleteSession: (id: number) =>
    api(`/api/agent/sessions/${id}`, { method: "DELETE" }),
  messages: (id: number) =>
    api<AgentMessage[]>(`/api/agent/sessions/${id}/messages`),
  send: (id: number, content: string) =>
    api(`/api/agent/sessions/${id}/messages`, { method: "POST", body: { content } }),
  tick: (id: number) =>
    api(`/api/agent/sessions/${id}/tick`, { method: "POST" }),
};
