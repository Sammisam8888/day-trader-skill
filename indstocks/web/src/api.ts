// Shapes returned by indstocks/dashboard.py. Numbers arrive as JSON
// numbers (Decimals converted server-side); missing values are null.

export type Status = "live" | "paper" | "shadow" | "halted" | "login";

export interface Session {
  phase: string; open: string; or_end: string; last_entry: string;
  flat_by: string; close: string; special: boolean;
}
export interface Position {
  symbol: string; token: string; qty: number; avg: number; ltp: number;
  invested: number; current: number; pnl: number; pct: number;
  source: "ai" | "manual"; stop: number | null; stop_live: boolean;
  target: number | null; entry: number | null; why: string;
}
export interface Plan {
  why?: string; stop?: string; target?: string; risk?: string; gross_if_target?: string;
  costs?: string; net_if_target?: string; position_value?: string; leverage?: string;
  decided_by?: string; confidence?: number;
}
export interface Order {
  id: string; symbol: string; token: string; side: "B" | "S"; type: string;
  qty: number; filled: number; avg: number; price: number; trigger: number;
  status: string; pending: boolean; time: string; reason: string;
  label: string; tone: "wait" | "fill" | "dead"; kind: string;
  source: "ai" | "manual"; ltp: number | null; plan: Plan | null;
}
export interface Closed {
  symbol: string; token: string; qty: number; buy_avg: number; sell_avg: number;
  gross: number; charges: number; net: number;
}
export interface Day { day: string; net: number | string | null; trades: number; wins: number }
export interface LogEvent { ts: string; job?: string; event: string; [k: string]: unknown }
export interface ChatMsg { ts: string; role: string; text: string }

export interface State {
  now: string; day: string; session: Session | null; status: Status; halt_reason: string;
  pip?: { streak: number; needed: number; min: string; days: Record<string, string> } | null;
  login: { ok: boolean; since: string | null; uid: string | null; configured: boolean; auto: boolean };
  data: { source: string; age: number | null; error: string };
  cash: number | null;
  symbols: { symbol: string; token: string | null; ltp: number | null }[];
  limits: Record<string, string>;
  pnl: { realised: number; open: number; charges: number; net: number };
  series: { t: string; net: number; realised: number; open: number }[];
  stats: { today_trades: number; today_wins: number; win_rate: number | null;
           trades: number; wins: number; history: Day[] };
  positions: Position[]; closed: Closed[]; orders: Order[];
  decisions: LogEvent[]; chat: ChatMsg[]; chat_mode: string;
  error?: string;
}

export interface Candles {
  symbol: string; tick?: number; error: string;
  candles: { time: number; open: number; high: number; low: number; close: number; volume: number }[];
  levels: { range_high?: number; range_low?: number; trigger?: number; stop?: number; target?: number };
}

export async function getState(): Promise<State> {
  const r = await fetch("/api/state", { cache: "no-store" });
  if (!r.ok) throw new Error(`state ${r.status}`);
  return r.json();
}

export async function getCandles(symbol: string): Promise<Candles> {
  const r = await fetch(`/api/candles?symbol=${encodeURIComponent(symbol)}`, { cache: "no-store" });
  return r.json();
}

export async function act(action: string, body: Record<string, unknown> = {}):
    Promise<{ ok: boolean; message: string }> {
  const r = await fetch("/api/action", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...body }),
  });
  return r.json();
}

export interface BrainAction { decision: number; outcome: string; reason: string; order_id?: string; order_status?: string | null }
export interface BrainDecision { action: string; symbol?: string; side?: string; order?: string; entry?: number;
  stop?: number; target?: number; size?: number; confidence?: number; reason?: string; order_id?: string;
  setup?: string; valid_minutes?: number }
export interface BrainCall {
  id: string; ts: string; source: string; phase: string; latency_s: number | null; error: string | null;
  asked: { symbols: number; cash: string; exposure: string; positions: number; resting: number; pnl: string;
    direction?: { state: string; score: number; allowed: string[]; why: string } | null };
  view: string; decisions: BrainDecision[]; reply: string; actions: BrainAction[]; prompt_chars: number;
}
export interface Trade {
  error?: string; order_id: string; symbol: string; side: "B" | "S"; status: string;
  decided_by: string; call_id: string; why: string; confidence: number | null; placed_at: string;
  plan: Record<string, string | number | null>;
  context?: { setup?: string | null; direction?: string | null; sentiment_bias?: number | null;
    sentiment_note?: string | null; advisory?: string[] | null; valid_minutes?: number | null; size_mult?: string | null };
  entry_order: Record<string, unknown> | null; stop_order: Record<string, unknown> | null;
  stop_moves: { ts: string; from: string; to: string; reason: string }[];
  exit: { ts: string; ltp: string; target: string; order: Record<string, unknown> | null } | null;
  ltp: number | null; live_pnl: number | null; realised: { gross: number; charges: number; net: number } | null;
}

export async function getBrain(day: string): Promise<{ day: string; calls: BrainCall[] }> {
  return (await fetch(`/api/brain?day=${day}`, { cache: "no-store" })).json();
}
export async function getBrainCall(day: string, id: string): Promise<{ prompt: string; reply: string }> {
  return (await fetch(`/api/brain/call?day=${day}&id=${id}`, { cache: "no-store" })).json();
}
export async function getTrade(day: string, id: string): Promise<Trade> {
  return (await fetch(`/api/trade?day=${day}&id=${encodeURIComponent(id)}`, { cache: "no-store" })).json();
}

export interface SentimentSnap {
  ts: string;
  positioning?: { vix?: { level: number; change_pct: number | null }; nifty?: { level: number; change_pct: number | null };
    options?: { expiry_symbol: string; atm: number; pcr: number | null; call_wall: number; put_wall: number;
      call_oi_added: number; put_oi_added: number }; vix_error?: string; options_error?: string };
  flows?: { fii?: { net_cr: number; date: string }; dii?: { net_cr: number; date: string }; error?: string };
  events?: { market: { what: string; at?: string; window: boolean }[]; stocks: Record<string, { what: string; results: boolean }>; error?: string };
  news?: { market?: { score: number; summary: string; drivers: string[] };
    stocks?: Record<string, { score: number; material: boolean; why: string }> };
  news_ts?: string; news_error?: string; news_errors?: string[];
  headlines?: { t: string; src: string; h: string }[];
  tilt: { bias: number; size: number; blocked: Record<string, Record<string, string>>; why: string };
}
export interface Score { trades: number; wins: number; net: number }
export interface LiveItem { ts: string; event: string; symbol: string; score?: number; headline?: string; source?: string; why?: string; move_pct?: string }
export interface SentimentResp {
  live?: LiveItem[]; headlines?: { ts: string; source: string; title: string; matched: string[] }[];
  snapshot: SentimentSnap | null; fresh: boolean;
  history: { ts: string; bias: number; vix: number | null; pcr: number | null }[];
  direction: { ts: string; state: string; score: number }[];
  scorecard: { sentiment: Record<string, Score>; direction: Record<string, Score>; setup?: Record<string, Score> };
}
export async function getSentiment(): Promise<SentimentResp> {
  return (await fetch("/api/sentiment", { cache: "no-store" })).json();
}

export interface ControlHist { ts: string; by: string; command: string; args: Record<string, unknown>; reason: string; outcome: string; message: string }
export interface ControlState {
  day_mode?: { flipped: boolean; halted: string | null; losers: { normal: number; flip: number }; mirror_pnl: string;
    flip_enabled: boolean; flip_after: number; flip_halt_after: number; stop_after: number; flipped_at?: string | null } | null;
  halt: { by: string; reason: string; ts: string } | null;
  risk: Record<string, string>; raised: Record<string, string>; history: ControlHist[];
  upcoming: { date: string; dow: string; trading: boolean; special: string | null; events: string[] }[];
  nse_synced: string | null; ceilings: Record<string, string | null>; base: Record<string, string>;
}
export async function getControl(): Promise<ControlState> {
  return (await fetch("/api/control", { cache: "no-store" })).json();
}

export interface PaperModel { net: string; realised: string; open: string; trades: number; wins: number; open_trades: number; killed: boolean; would_kill?: { t: number; net: string } | null }
export interface PaperTrade { ev: string; t: number; model: string; symbol: string; side: string; qty: number; entry: number; exit?: number; net?: string; why?: string; stop?: number; target?: number; opened?: number }
export interface PaperAI { at?: string; day_read?: string; models: Record<string, Record<string, number | string>> }
export interface PaperResp { day: string; ai?: PaperAI | null; summary: { total: string; at: number; models: Record<string, PaperModel> } | null; trades: PaperTrade[] }
export async function getPaper(): Promise<PaperResp> {
  return (await fetch("/api/paper", { cache: "no-store" })).json();
}

export interface DeskRow { symbol: string; sector: string; index: string; traded: boolean; ltp: number | null; chg_1m: number | null; chg_5m: number | null;
  vol_x: number | null; upticks: number | null; imbalance: number | null; spread_bps: number | null; momentum: number | null; updated: number | null }
export interface DeskResp { day: string; monitor: DeskRow[]; internals: { advances?: number; declines?: number; unchanged?: number; sectors?: Record<string, number> } | null;
  news: { ts?: string; event?: string; symbol?: string; score?: number; headline?: string; source?: string; move_pct?: number }[];
  ai: { ts?: string; view?: string } | null; bench: { model: string; net: number; trades: number; wins: number }[];
  company?: Record<string, { holding?: { as_of: string; promoter_pct: number; public_pct: number | null; promoter_change: number | null };
    results?: { filed: string; relating_to?: string; quarter_to?: string; days_ago: number }; deals?: DeskDeal[] }>;
  deals?: { as_on?: string; ours?: DeskDeal[]; biggest?: DeskDeal[] } }
export interface DeskDeal { kind: string; symbol: string; side?: string; client: string; qty: number | null; price: number | null; value_cr: number | null }
export async function getDesk(): Promise<DeskResp> {
  return (await fetch("/api/terminal", { cache: "no-store" })).json();
}

export interface StratToday { net: number; trades: number; wins: number; open: number; target_hit: number | string | null }
export interface StratRow { name: string; kind: "fixed" | "inverted" | "ai_tuned" | "ai_trader"; recipe: string; wallet: number; target: number;
  today: StratToday; history: { day: string; net: number; trades: number | null }[]; learning: { day: string; text: string } | null;
  days: number; total: number; green_days: number; target_days: number }
export interface StratOverview { day: string; wallet: number; target: number; total_today: number; strategies: StratRow[] }
export interface StratTrade { stock: string; side: string; qty: number; opened?: string | null; closed?: string | null; entry?: number;
  exit?: number | null; stop?: number; target?: number; net?: number | null; why?: string; time?: string; event?: string; price?: string; note?: string }
export interface LogRow { time: string; event: string; stock: string; side: string; qty: string; price: string; stop: string; target: string; net: string; note: string }
export interface StratDetail { name: string; day: string; kind: string; recipe: string; summary: StratRow | null; trades: StratTrade[];
  log_rows: LogRow[]; log_md: string; learning_md: string; days: string[];
  knobs: { values: Record<string, number> | null; why?: string; set_at?: string; day_read?: string; ranges: Record<string, [number, number]>; doc: string } | null }
export interface Stage { key: string; name: string; what: string; last: string | null; age_s: number | null; status: "ok" | "late" | "down" | "idle" }
export interface PipelineResp { at: string; broker: string; stages: Stage[] }
export async function getStrategies(day?: string): Promise<StratOverview> {
  return (await fetch(`/api/strategies${day ? `?day=${day}` : ""}`, { cache: "no-store" })).json();
}
export async function getStrategy(name: string, day?: string): Promise<StratDetail> {
  return (await fetch(`/api/strategy?name=${encodeURIComponent(name)}${day ? `&day=${day}` : ""}`, { cache: "no-store" })).json();
}
export async function getPipeline(): Promise<PipelineResp> {
  return (await fetch("/api/pipeline", { cache: "no-store" })).json();
}
