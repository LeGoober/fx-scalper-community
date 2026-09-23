// api.ts: the typed contract between the UI and the FastAPI backend.
// Written from the live routers (see docs/openapi.json). UI code must call the backend ONLY through
// this file. If an endpoint changes, change it here first. Zero dependencies: native fetch + WebSocket.

export const API_BASE: string = (globalThis as any).FXS_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) { super(message); }
}

async function call<T>(method: string, path: string, body?: unknown, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    ...init,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const msg = (data && (data.detail ?? data.error)) || res.statusText;
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg), data);
  }
  return data as T;
}
const get = <T>(p: string) => call<T>("GET", p);
const post = <T>(p: string, b?: unknown) => call<T>("POST", p, b ?? {});
const put = <T>(p: string, b: unknown) => call<T>("PUT", p, b);
const del = <T>(p: string) => call<T>("DELETE", p);
const qs = (o: Record<string, unknown>) => {
  const e = Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return e.length ? "?" + new URLSearchParams(e.map(([k, v]) => [k, String(v)])).toString() : "";
};

// ───────────────────────────────────────────────────────────── shared types
export type Epoch = number;            // seconds since 1970 UTC
export type ISO = string;              // "2026-09-23T17:16:16Z"
export type Direction = "long" | "short";
export type TradeMode = "paper" | "demo" | "real";
export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Candle { epoch: Epoch; open: number; high: number; low: number; close: number }

export interface Summary {                 // trade statistics in R (same maths everywhere)
  trades: number;
  win_rate?: number; avg_win_r?: number; avg_loss_r?: number; payoff?: number | null;
  expectancy_r?: number; breakeven_win_rate?: number | null; total_r?: number;
  profit_factor?: number | null; max_drawdown_r?: number; max_consecutive_losses?: number;
  sharpe_per_trade?: number | null; t_stat?: number | null;
  expectancy_ci95?: [number, number]; p_expectancy_le_0?: number;
}
export interface EquityPoint { t: ISO | null; r: number; balance: number }

export interface Job<R = unknown> {
  id: string; kind: "backfill" | "transcripts" | "tagging" | "backtest" | string; status: JobStatus;
  params: Record<string, unknown>; progress: Record<string, any>; result: R | null; error: string | null;
  created_at: ISO; finished_at: ISO | null;
}

// ─────────────────────────────────────────────────────────────── system
export interface Health { ok: boolean; service: string; broker_provider: string; api: string }
export const health = () => get<Health>("/health");

export interface SecretStatus {
  name: string; label: string; group: "deriv" | "jev" | "tradingview" | "openbb" | "notifications";
  secret: boolean; help: string; is_set: boolean; value: string | null;  // value only for non-secret config
}
export const secrets = {
  list: () => get<{ secrets: SecretStatus[]; env_file: string }>("/secrets"),
  set: (name: string, value: string) => put<{ name: string; is_set: true }>(`/secrets/${name}`, { value }),
  clear: (name: string) => del<{ name: string; is_set: false }>(`/secrets/${name}`),
  test: (name: string) => post<{ ok: boolean; detail: unknown; latency_ms: number }>(`/secrets/${name}/test`),
  rotateTradingView: () => post<{ name: string; value: string; note: string }>(
    "/secrets/tradingview_webhook_secret/rotate"),   // value is shown ONCE; never stored in the UI
};

export interface RiskLimits {
  max_stake: number; max_risk_per_trade: number; max_daily_loss: number; max_concurrent: number;
  max_orders_per_day: number;
}
export interface RiskState {
  limits: RiskLimits;
  today: { day: string; realised_loss: number; realised_pnl: number; orders: number; open: number };
  kill_switch: boolean;
  real_trading: { env_allows: boolean; armed: boolean; effective: boolean };
}
export const risk = {
  get: () => get<RiskState>("/risk"),
  setLimits: (limits: Partial<RiskLimits>) => put<{ limits: RiskLimits }>("/risk/limits", limits),
  killSwitch: (active: boolean) => post<{ kill_switch: boolean }>("/risk/kill-switch", { active }),
  // Real money: only works if the user hand-edited COMMUNITY_ALLOW_REAL_TRADING=true in .env.
  armReal: (confirm_phrase: string) => post<RiskState["real_trading"]>("/risk/real/arm", { confirm_phrase }),
  disarmReal: () => post<RiskState["real_trading"]>("/risk/real/disarm"),
};
export const REAL_MONEY_PHRASE = "I ACCEPT REAL MONEY RISK";

export const jobs = {
  list: (kind?: string) => get<{ jobs: Job[] }>(`/jobs${qs({ kind })}`),
  get: (id: string) => get<Job>(`/jobs/${id}`),
  cancel: (id: string) => post<{ cancelled: boolean }>(`/jobs/${id}/cancel`),
};

export interface EventRow { id: number; ts: ISO; kind: string; level: "debug" | "info" | "warning" | "error";
  message: string; data: any }
export const eventsLog = (limit = 100, kind = "") => get<{ events: EventRow[] }>(`/events${qs({ limit, kind })}`);

// ─────────────────────────────────────────────────────────── market data
export interface SymbolInfo { symbol: string; name: string; market: string; submarket: string; open: boolean;
  pip_size: number; ict_suitable: boolean; ict_default: boolean }
export interface CoverageSeries { symbol: string; granularity: number; bars: number; first: Epoch; last: Epoch;
  largest_gaps: { from_epoch: Epoch; to_epoch: Epoch; seconds: number }[] }
export const market = {
  symbols: (market_?: string) => get<{ symbols: SymbolInfo[]; ict_defaults: string[] }>(
    `/market/symbols${qs({ market: market_ })}`),
  contracts: (symbol: string) => get<{ symbol: string; contract_types: string[]; multipliers: number[] }>(
    `/market/contracts/${symbol}`),
  backfill: (b: { symbol: string; granularity?: number; days?: number; start?: Epoch; end?: Epoch }) =>
    post<Job<{ fetched: number; written: number; errors: string[] }>>("/market/backfill", b),
  coverage: () => get<{ series: CoverageSeries[] }>("/market/coverage"),
  candles: (symbol: string, o: { granularity?: number; start?: Epoch; end?: Epoch; resample?: number;
    limit?: number } = {}) => get<{ symbol: string; granularity: number; candles: Candle[] }>(
    `/market/candles/${symbol}${qs(o)}`),
  openbbStatus: () => get<{ installed: boolean; version: string | null; loaded: boolean; import_error: string | null;
    keys: Record<string, boolean>; calendar_provider: string; free_sources: string[] }>("/market/openbb/status"),
  openbbHtf: (pair: string, o: { interval?: string; days?: number; provider?: string } = {}) =>
    get<{ pair: string; interval: string; source: string; candles: Candle[] }>(`/market/openbb/htf/${pair}${qs(o)}`),
  calendar: (o: { days_ahead?: number; days_back?: number; provider?: string; min_importance?: number } = {}) =>
    get<{ provider: string; fallback_reason: string | null; fetched: number; events: EconEvent[] }>(
      `/market/openbb/calendar${qs(o)}`),
  news: (o: { query?: string; limit?: number; provider?: string } = {}) =>
    get<{ provider: string; items: { date: string; title: string; url: string; source: string }[] }>(
      `/market/openbb/news${qs(o)}`),
  crosscheck: (symbol: string, days = 60) => get<{ symbol: string; compared: number;
    mean_abs_diff_bps?: number | null; max_abs_diff_bps?: number | null; note?: string }>(
    `/market/openbb/crosscheck/${symbol}${qs({ days })}`),
};
export interface EconEvent { ts: Epoch; currency: string; title: string; importance: 0 | 1 | 2 | 3; source: string;
  actual: string | null; forecast: string | null; previous: string | null }

// ─────────────────────────────────────────────────────────── transcripts
export interface Video { video_id: string; title: string | null; channel: string | null; upload_date: string | null;
  duration_s: number | null; playlist_id: string | null; url: string;
  status: "pending" | "ok" | "failed" | "blocked"; error: string | null; language: string | null;
  segment_count: number; fetched_at: ISO | null; source: string | null; tagged_windows: number }
export interface Segment { idx: number; start: number; duration: number; text: string; url: string }
export interface PullStats { source: { kind: string; id: string; title: string; channel: string }; total: number;
  done: number; ok: number; skipped: number; failed: number; blocked: boolean;
  failures: { video_id: string; error: string }[] }
export const transcripts = {
  sources: () => get<{ default_url: string; default_title: string;
    fallbacks: { name: string; url: string; notes: string }[] }>("/transcripts/sources"),
  preview: (url: string) => post<{ kind: string; id: string; title: string; channel: string;
    videos: { video_id: string; title: string; duration_s: number; stored: string | null }[] }>(
    "/transcripts/preview", { url }),
  pull: (b: { url?: string; limit?: number; delay_s?: number; force?: boolean }) =>
    post<Job<PullStats>>("/transcripts/jobs", b),
  jobs: () => get<{ jobs: Job<PullStats>[] }>("/transcripts/jobs"),
  import: (b: { video_id: string; format: "srt" | "vtt" | "json" | "txt"; content: string; title?: string;
    channel?: string }) => post<{ video_id: string; segments: number }>("/transcripts/import", b),
  videos: (o: { status?: string; q?: string } = {}) => get<{ videos: Video[]; totals: { videos: number; ok: number;
    failed: number; blocked: number; segments: number } }>(`/transcripts/videos${qs(o)}`),
  video: (id: string, o: { offset?: number; limit?: number } = {}) =>
    get<{ video: Video; segments: Segment[] }>(`/transcripts/videos/${id}${qs(o)}`),
  search: (q: string, limit = 50) => get<{ q: string; hits: { video_id: string; start: number; text: string;
    title: string; url: string }[] }>(`/transcripts/search${qs({ q, limit })}`),
};

// ───────────────────────────────────────────────────── strategy & Jev
export type NodeKind = "trigger" | "code" | "jev";
export interface Citation { video_id: string; t: number; title: string | null; note: string }
export interface StrategyNode { id: string; label: string; kind: NodeKind; description: string;
  detector: string | null; params: Record<string, unknown>;
  question: { type: "choice" | "score" | "noul"; instructions: string; criteria?: unknown } | null;
  gate: Record<string, unknown>; fallback: string | null; on_fail: "reject" | "flag"; citations: Citation[] }
export interface StrategySchema { id: string; version: number; name: string; model: string; description: string;
  status: "draft" | "review" | "approved"; symbols: string[]; entry_granularity: number;
  scan: Record<string, unknown>; nodes: StrategyNode[];
  execution: { entry: "fvg_ce" | "fvg_edge"; stop_buffer_atr: number;
    target: { mode: "liquidity" | "fixed_r"; r: number; min_rr: number; max_r: number };
    entry_window_bars: number; max_trades_per_day: number; session_exit_ny: string | null;
    contract: { type: "multiplier" | "rise_fall"; multiplier: number; rise_fall_minutes: number;
      rise_fall_payout: number }; risk_per_trade_pct: number };
  jev_model_pin: string | null; sources: Record<string, unknown>[]; authored_by: string; changelog: string[] }
export interface GraphNode { id: string; kind: NodeKind | "start" | "plan" | "risk" | "order"; label: string;
  description?: string; question_type?: string | null; gate?: Record<string, unknown>; on_fail?: string;
  citations?: Citation[] }
export interface TaggedPassage { video_id: string; window_idx: number; start: number; end: number; text: string;
  concept: string; concept_confidence: number; is_rule: number; specificity: number; bias: string;
  bias_confidence: number; model: string; tagged_at: ISO; title: string; execution: number | null; url: string }
export const strategy = {
  concepts: () => get<{ concepts: { id: string; description: string; windows: number | null;
    rule_windows: number | null; avg_specificity: number | null }[]; questions: Record<string, unknown> }>(
    "/strategy/concepts"),
  tag: (b: { video_ids?: string[]; limit?: number; retag?: boolean } = {}) =>
    post<Job<{ videos: number; windows: number; tagged: number; cached: number; errors: number;
      input_tokens: number; model: string }>>("/strategy/tagging/jobs", b),
  tags: (o: { concept?: string; min_rule?: number; limit?: number } = {}) =>
    get<{ passages: TaggedPassage[] }>(`/strategy/tags${qs(o)}`),
  digest: () => post<{ path: string; passages: number; concepts: unknown[] }>("/strategy/digest"),
  jevUsage: () => get<{ cached_calls: number; input_tokens: number; estimated_cost_usd: number;
    avg_latency_ms: number | null; last_call: ISO | null; models: { model: string; calls: number }[];
    configured_model: string; key_set: boolean }>("/strategy/jev/usage"),
  jevTest: () => post<{ ok: boolean; detail: unknown }>("/strategy/jev/test"),
  schemas: () => get<{ strategies: { id: string; version: number; name: string; created_at: ISO; status: string;
    symbols: string[]; nodes: number; entry_granularity: number }[] }>("/strategy/schemas"),
  schema: (id: string, version?: number) => get<StrategySchema>(`/strategy/schemas/${id}${qs({ version })}`),
  graph: (id: string, version?: number) => get<{ nodes: GraphNode[]; edges: { from: string; to: string;
    kind: "pass" }[] }>(`/strategy/schemas/${id}/graph${qs({ version })}`),
  mermaidUrl: (id: string, version?: number) => `${API_BASE}/strategy/schemas/${id}/mermaid${qs({ version })}`,
};

// ─────────────────────────────────────────────────────────── backtests
export interface BacktestRequest { strategy_id?: string; version?: number; symbol: string; days?: number;
  start?: Epoch; end?: Epoch; mode?: "code" | "jev" | "compare"; oos_fraction?: number; start_balance?: number;
  cost?: number; session_exit?: boolean; overrides?: Record<string, unknown> }
export interface BacktestTrade { entry_time: ISO; exit_time: ISO; entry: number; exit: number; stop: number | null;
  target: number | null; r: number; gross_r: number; cost_r: number; exit_reason: string; bars_held: number;
  direction: Direction; killzone: string; liquidity: string; rr_planned: number; target_name: string;
  flags: string[]; nodes: Record<string, unknown>; side: "BUY" | "SELL" }
export interface ModeResult { summary: Summary;
  execution_funnel: { setups: number; passed_filters: number; skipped_position_open: number;
    skipped_daily_cap: number; not_filled: number; missed_target_first: number; filled: number };
  validation: { in_sample?: Summary; out_of_sample?: Summary; oos_starts?: ISO | null;
    folds?: { fold: number; from: ISO; to: ISO; trades: number; expectancy_r: number | null;
      win_rate: number | null }[] };
  by_killzone: (Summary & { killzone: string })[]; by_direction: (Summary & { direction: string })[];
  by_exit: (Summary & { exit_reason: string })[];
  nodes: { id: string; label: string; kind: NodeKind; evaluated: number; passed: number;
    pass_rate: number | null }[]; setups_passed: number }
export interface BacktestRun { id: string; status: "running" | "done" | "failed"; created_at: ISO;
  finished_at: ISO | null; error: string | null; params: BacktestRequest & Record<string, unknown>;
  strategy: { id: string; version: number; name: string; overrides: unknown }; symbol: string; granularity: number;
  bars: number; from: ISO; to: ISO; setups: number; cost_price: number; contract: string; warnings: string[];
  results: Record<"code" | "jev", ModeResult>; equity: Record<string, EquityPoint[]>;
  trades?: Record<string, BacktestTrade[]>; elapsed_s: number }
export const backtests = {
  run: (b: BacktestRequest) => post<Job<{ id: string; headline: Record<string, Summary> }>>("/backtests/run", b),
  list: (limit = 50) => get<{ runs: { id: string; created_at: ISO; finished_at: ISO | null; status: string;
    error: string | null; params: BacktestRequest; headline: Record<string, Partial<Summary>> }[] }>(
    `/backtests${qs({ limit })}`),
  get: (id: string, include_trades = true) => get<BacktestRun>(`/backtests/${id}${qs({ include_trades })}`),
};

// ─────────────────────────────────────────────────────────────── trading
export interface EngineConfig { strategy_id?: string; version?: number | null; symbols: string[];
  mode: "paper" | "demo"; evaluation: "code" | "jev"; risk_amount: number; currency?: string }
export interface EngineStatus { running: boolean; started_at: ISO | null; stopped_reason: string | null;
  config: EngineConfig | null; strategy: { id: string; version: number } | null; feed_rtt_ms: number | null;
  broker: { connected: boolean; authorized: boolean; token_mode: string; loginid: string | null;
    is_virtual: boolean | null; endpoint: string; account_type: string | null; currency: string | null;
    balance: number | null; rtt_ms: number | null } | null;
  symbols: { symbol: string; bars: number; last_price: number | null; last_tick: Epoch | null;
    pending: { id: string; direction: Direction; entry: number; stop: number; target: number;
      expires_at: Epoch }[];
    position: { trade_id: string; direction: Direction; entry: number; stop: number; target: number;
      risk_amount: number; opened_at: Epoch; contract_id: string | null; mode: string } | null;
    scans: number; last_scan: Epoch | null;
    evaluations: number; signals: number; errors: number; last_error: string | null }[] }
export interface ContractProfile { symbol: string; calibrated_at: ISO; categories?: string[];
  multipliers_advertised?: number[]; multipliers?: number[]; quoted_multiplier?: number; commission_rate?: number;
  cost_price?: number; spot?: number; rise_fall?: { minutes: number; payout_r: number; breakeven_win_rate: number }
  | { error: string }; executable?: { multiplier: boolean; rise_fall: boolean }; error?: string }
export interface Signal { id: string; created_at: ISO; symbol: string; direction: Direction; strategy_id: string;
  strategy_version: number; source: string; status: "accepted" | "rejected";
  decision: { passed: boolean; flags: string[]; plan: { direction: Direction; entry: number; stop: number;
    target: number; risk: number; rr: number; target_name: string } | null;
    nodes: { id: string; kind: string; passed: boolean; value: unknown; detail: string; source: string }[];
    setup: { sweep: string; armed_at: Epoch; killzone: string | null } } }
export interface Trade { id: string; mode: TradeMode; symbol: string; direction: Direction; contract_type: string;
  stake: number | null; entry_price: number | null; exit_price: number | null; stop_price: number | null;
  target_price: number | null; r_multiple: number | null; pnl: number | null; status: "open" | "closed";
  opened_at: ISO; closed_at: ISO | null; contract_id: string | null; signal_id: string | null;
  strategy_id: string | null; meta: Record<string, unknown> }
export const trading = {
  engine: () => get<EngineStatus>("/trading/engine"),
  start: (cfg: EngineConfig) => post<EngineStatus>("/trading/engine/start", cfg),
  stop: () => post<EngineStatus>("/trading/engine/stop"),
  calibrate: (symbols?: string[]) => post<{ profiles: Record<string, ContractProfile> }>("/trading/calibrate",
    symbols ? { symbols } : {}),
  profiles: () => get<{ profiles: Record<string, ContractProfile> }>("/trading/profiles"),
  signals: (o: { limit?: number; status?: "accepted" | "rejected" } = {}) =>
    get<{ signals: Signal[] }>(`/trading/signals${qs(o)}`),
  trades: (o: { limit?: number; mode?: TradeMode; status?: "open" | "closed" } = {}) =>
    get<{ trades: Trade[] }>(`/trading/trades${qs(o)}`),
  tradesCsvUrl: (mode?: TradeMode) => `${API_BASE}/trading/trades.csv${qs({ mode })}`,
};

// ─────────────────────────────────────────────────────────────── metrics
export const metrics = {
  overview: () => get<{
    performance: { modes: Record<TradeMode, { summary: Summary; equity: EquityPoint[]; pnl: number }> };
    execution: { deriv_feed_rtt_ms: number | null; broker: EngineStatus["broker"];
      jev: { calls: number; avg_latency_ms: number | null; max_latency_ms: number | null };
      entry_slippage_r: { fills: number; avg: number | null } };
    strategy: { signals: Record<string, number>; killzones: Record<string, number>;
      nodes: { id: string; evaluated: number; passed: number; pass_rate: number | null }[] };
    data: { candles: Omit<CoverageSeries, "largest_gaps">[];
      transcripts: { videos: number; ok: number; problems: number; segments: number };
      jev_tags: { windows: number; videos: number };
      calendar: { events: number; first: Epoch | null; last: Epoch | null } };
    risk: RiskState; last_backtest: { id: string; created_at: ISO; symbol: string;
      results: Record<string, Partial<Summary>> } | null; engine: { running: boolean } }>("/metrics/overview"),
  performance: (o: { mode?: TradeMode; start_balance?: number; risk_pct?: number } = {}) =>
    get<{ modes: Record<string, { summary: Summary; equity: EquityPoint[]; pnl: number }> }>(
      `/metrics/performance${qs(o)}`),
};

// ─────────────────────────────────────────────────────── legacy (old Vue UI)
export const legacy = { bootstrap: () => get<Record<string, unknown>>("/bootstrap") };

// ─────────────────────────────────────────────────────────── live stream
// One WebSocket for everything live. Message: { type, ts, level, message, data }.
// Types the UI should handle:
//   stream.hello | stream.heartbeat              connection housekeeping
//   stream.job.progress   data: Job              progress bars for backfill/transcripts/tagging/backtest
//   job.started | job.done | job.failed | job.cancelled   data: Job
//   engine.started | engine.stopped | engine.feed_error
//   signal.new (data: {id, symbol, direction, status, plan, flags}) | signal.expired
//   trade.opened | trade.closed (data: {id, mode, symbol, r, pnl, reason}) | trade.rejected | trade.tracking_error
//   risk.kill_switch | risk.blocked | risk.limits | risk.real_armed | risk.real_disarmed
//   broker.connected | broker.error | backtest.done | webhook.tradingview | webhook.rejected | system.secret_set
export interface StreamMessage<T = any> { type: string; ts: ISO | null; level: string; message: string; data: T }

export function connectStream(onMessage: (m: StreamMessage) => void,
                              onState?: (s: "open" | "closed") => void): () => void {
  let ws: WebSocket | null = null, stopped = false, retry = 1000;
  const url = (() => {
    const base = new URL(API_BASE, location.href);
    base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    return base.href.replace(/\/$/, "") + "/stream";
  })();
  const open = () => {
    ws = new WebSocket(url);
    ws.onopen = () => { retry = 1000; onState?.("open"); };
    ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch { /* ignore malformed */ } };
    ws.onclose = () => { onState?.("closed"); if (!stopped) setTimeout(open, retry = Math.min(retry * 2, 15000)); };
  };
  open();
  return () => { stopped = true; ws?.close(); };
}
