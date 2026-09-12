/**
 * ZUMVIA — API istemcisi
 * Token localStorage'da tutulur; her istekte Bearer olarak eklenir.
 */

export type Bot = {
  id: number;
  name: string;
  market: string;
  exchange: string;
  symbol: string;
  timeframe: string;
  mode: "paper" | "live";
  autonomy: "manual" | "semi" | "full";
  decision_mode: "hybrid" | "ai_first" | "algo_only" | "ai_only";
  status: "stopped" | "running" | "locked" | "error";
  strategies: string[];
  min_agree: number;
  llm_model: string;
  llm_credential_id: number | null;
  risk: {
    risk_pct: number;
    effective_risk_pct: number;
    daily_loss_limit_pct: number;
    min_confidence: number;
    effective_min_confidence: number;
    min_rr: number;
    max_open_positions: number;
    max_drawdown_pct: number;
    recovery_mode: boolean;
    consecutive_losses: number;
    locked_until: string | null;
    lock_reason: string;
  };
  capital: {
    initial_balance: number;
    balance: number;
    peak_equity: number;
    realized_pnl: number;
    total_return_pct: number;
  };
  stats: {
    total_trades: number;
    wins: number;
    losses: number;
    win_rate_pct: number;
    open_positions: number;
  };
  last_run_at: string | null;
  next_run_at: string | null;
};

export type Position = {
  id: number;
  symbol: string;
  side: "long" | "short";
  status: "open" | "closed" | "pending" | "rejected";
  qty: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  exit_price: number | null;
  pnl: number;
  pnl_pct: number;
  r_multiple: number;
  unrealized_pnl: number | null;
  confidence: number;
  reasoning: string;
  close_reason: string;
  opened_at: string | null;
  closed_at: string | null;
};

export type BotEvent = {
  id: number;
  ts: string;
  level: "info" | "success" | "warn" | "error" | "trade";
  category: string;
  message: string;
  data: Record<string, unknown>;
};

const TOKEN_KEY = "vq_token";

export const auth = {
  get token() {
    return typeof window === "undefined" ? "" : localStorage.getItem(TOKEN_KEY) || "";
  },
  set(token: string, email: string) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem("vq_email", email);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem("vq_email");
  },
  get email() {
    return typeof window === "undefined" ? "" : localStorage.getItem("vq_email") || "";
  },
};

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

export async function api<T = unknown>(
  path: string,
  options: { method?: string; body?: unknown; auth?: boolean } = {},
): Promise<T> {
  const { method = "GET", body, auth: useAuth = true } = options;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (useAuth && auth.token) headers.Authorization = `Bearer ${auth.token}`;

  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 401 && useAuth) {
    auth.clear();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new ApiError("Oturum süresi doldu.", 401);
  }

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;

  if (!res.ok) {
    const detail = data?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d: { msg: string }) => d.msg).join(", ")
          : `İstek başarısız (${res.status})`;
    throw new ApiError(message, res.status);
  }
  return data as T;
}

export const fetcher = <T,>(path: string) => api<T>(path);

/** Canlı olay akışı — bileşenler bu köprüye abone olur. */
export function openEventStream(onEvent: (event: Record<string, any>) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const apiBase = process.env.NEXT_PUBLIC_API_URL || window.location.origin;
  const url = new URL(apiBase);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/stream";
  url.searchParams.set("token", auth.token);

  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const connect = () => {
    if (closed) return;
    socket = new WebSocket(url.toString());
    socket.onmessage = (raw) => onEvent(JSON.parse(raw.data));
    socket.onclose = () => {
      socket = null;
      if (!closed) reconnectTimer = setTimeout(connect, 4000);
    };
  };
  connect();

  return () => {
    closed = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    socket?.close();
    socket = null;
  };
}

/* ------------------------------- biçimleme ------------------------------- */

export const money = (v: number, digits = 2) =>
  Number(v ?? 0).toLocaleString("tr-TR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });

export const pct = (v: number) => `${(v ?? 0) >= 0 ? "+" : ""}${Number(v ?? 0).toFixed(2)}%`;

export const toneOf = (v: number) =>
  v > 0 ? "text-emerald" : v < 0 ? "text-danger" : "text-slate-400";

export const decisionModeLabel = (mode: string) =>
  ({
    hybrid: "Hibrit (YZ + Algoritma)",
    ai_first: "YZ Öncelikli",
    algo_only: "Sadece Algoritma",
    ai_only: "Sadece Yapay Zeka",
  })[mode] ?? mode;
