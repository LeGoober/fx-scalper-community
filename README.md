# FX Scalper: ICT × Jev × Deriv

A personal quant platform built on the **ICT (Inner Circle Trader) 2022 Mentorship model**:

1. YouTube transcripts are pulled.
2. **Jev** (TypeSafe's System One model) tags them by concept.
3. The tagged transcripts become a versioned, cited **strategy graph**, which is backtested on **Deriv** candles with real Deriv costs.
4. The strategy then runs live in **paper** or **Deriv demo** mode.

**OpenBB** supplies higher-timeframe data, the economic calendar and news, and **TradingView** mirrors the detectors on your charts.

> Fork of [Rasta-syntaX/fx-scalper-community](https://github.com/Rasta-syntaX/fx-scalper-community) (MIT). The original dashboard's logic is kept verbatim in `backend/app/legacy/`, and every original `/api` route still works.

⚠️ **Education and research tooling, not financial advice.** Nothing here is proven profitable: see [Current results](#current-results-honest). Trading can lose you money. The platform is **locked to demo accounts** by default.

---

## Architecture

```
YouTube ──yt-dlp + youtube-transcript-api──▶ transcripts (SQLite, gitignored)
                                               │  Jev: Choice/Score/Noul per 75 s window
                                               ▼
                                   concept digest ──Claude──▶ strategies/*.json (versioned, cited, diagrammed)
Deriv WS (public candles) ──▶ candles ──▶ causal ICT detectors ──▶ strategy runtime (code + Jev nodes)
OpenBB (HTF, calendar, news) ─────────────────────────────────────▲        │
                                                                           ├─▶ backtester (costs, funnel, OOS)
TradingView alert ──webhook──▶ re-check on fresh data ───────────────────▶ └─▶ live engine (paper | demo)
                                                                                    │ risk gate (fail-closed demo lock)
                                                               FastAPI + WebSocket ─┴─▶ vanilla TS UI (ui/)
```

| Path | What |
|---|---|
| `backend/app/main.py` | FastAPI app (68 REST routes + `/api/stream` WebSocket) |
| `backend/app/services/deriv/` | Async Deriv client (current API, PAT/OTP auth, `req_id` multiplexing), history backfill, contract calibration |
| `backend/app/services/transcripts/` | Transcript extraction and imports |
| `backend/app/services/jev/` | Jev client (cached, cost-tracked) and concept tagging |
| `backend/app/services/ict/` | Detectors (`features.py`), trigger scanner, strategy schema and runtime |
| `backend/app/services/backtest/` | Event-driven backtester and statistics |
| `backend/app/services/engine.py` | Live paper/demo engine |
| `backend/app/services/risk.py` | Order-time risk gate |
| `backend/strategies/` | Strategy schemas (`.json`) and generated diagrams (`.md`) |
| `tradingview/ict_2022_model.pine` | Pine v6 mirror plus webhook alerts |
| `ui/` | Vanilla TypeScript UI: `src/api.ts` is the typed contract, `docs/UI_BRIEF.md` the Kimi brief |
| `docs/openapi.json` | Exported API contract (`python -m app.cli openapi`) |

## Quick start (Windows, PowerShell)

```powershell
C:\Python\python.exe -m venv .venv          # Python 3.12 (OpenBB is happiest there)
.\.venv\Scripts\pip install -r backend\requirements-data.txt -r backend\requirements-dev.txt
copy .env.example .env                      # then set keys here or in the UI (write-only)
cd backend
..\.venv\Scripts\python -m app              # http://127.0.0.1:5001  (Swagger at /docs)
```

UI: `cd ui && npm install && npm run build`; the backend then serves it at `/`.

### CLI (same code paths as the API)
```powershell
python -m app.cli transcripts pull                       # 2022 ICT Mentorship playlist (resumable)
python -m app.cli candles backfill frxEURUSD --days 365  # Deriv keeps ~1 year of 1m candles
python -m app.cli strategy tag                           # needs TYPESAFE_API_KEY
python -m app.cli strategy diagram                       # writes strategies/*.md
python -m app.cli backtest run frxEURUSD --days 365 --mode compare
python -m app.cli openapi
```

## Safety model

- **Keys are write-only.** They're stored in `.env` (gitignored), and no endpoint returns a key; the API only reports whether each one is set. Create your Deriv token with **Read + Trade** scopes only.
- **The API listens on localhost only** (`127.0.0.1`). The original server bound `0.0.0.0` with Flask debug on.
- **Demo lock (fail-closed).** An order is sent only if every signal agrees the account is virtual: the server-issued OTP endpoint (`/ws/demo`) and the account metadata. If anything is unknown or contradictory, the order is refused.
- **Real money** needs all of the following:
  - `COMMUNITY_ALLOW_REAL_TRADING=true`, hand-edited into `.env` (the API cannot set it);
  - a typed confirmation phrase each session;
  - the same stake, risk, daily-loss and concurrency limits as demo;
  - a kill switch that stays reachable at all times.
- **TradingView alerts never trade directly.** They are authenticated and de-duplicated, then only ask the engine to re-evaluate on fresh Deriv data.

## Current results (honest)

Measured 2026-09-23 on 1 year of Deriv 1-minute data, 5-minute entry timeframe, code-only evaluation (Jev not yet enabled), with Deriv's real commission of 2 bps of notional:

| Strategy | Symbol | Contract | Trades | Expectancy | 95% CI |
|---|---|---|---:|---:|---|
| v1 | OTC_NDX | Rise/Fall 15m | 28 | +0.10R | [−0.22, +0.43] |
| v1 | EURUSD / GBPUSD | Multiplier | 2 / 2 | n/a | too few trades |
| v2 | OTC_NDX | Rise/Fall 15m | 10 | −0.09R | [−0.64, +0.46] |

**No statistically significant edge yet.** The data points to four limits:
- **Too little history.** Deriv keeps only about 1 year of 1-minute data, and the model makes a handful of trades a year per symbol.
- **Costs dominate.** On 5-minute forex, stops are a few pips, so commission is a large share of 1R.
- **No multipliers on indices.** Deriv's OTC indices offer Rise/Fall only, which needs a win rate above 55%.
- **Crude code stand-ins.** The code fallbacks for fuzzy judgments (daily draw on liquidity) are rough; that is where Jev comes in.

Next: Jev compare-mode backtests, finishing the transcript set, and longer history.

## Credits

Original Community Edition by the fx-scalper authors (MIT). ICT concepts are © Michael J. Huddleston (The Inner Circle Trader). Transcripts are fetched for personal research and never committed.
