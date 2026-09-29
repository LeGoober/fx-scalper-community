# Trading desk user guide

A personal ICT (Inner Circle Trader) trading desk: it watches Deriv prices, finds ICT 2022-model setups,
checks each one against a rule list, and either records the trade on paper, places it on your Deriv
demo account, or, only when you unlock it yourself, places it on your real account.

```
Deriv prices ──► setup scanner ──► rule checks (code + Jev) ──► confidence JSON ──► paper / demo / real order
                                                                        └──► alerts + Deriv ticket + Pine Script
```

The honest status is in [What the evidence says](#what-the-evidence-says-so-far). Read it before you put money in.

---

## 1. Start and stop

From the `OpenBB` folder:

```bash
npm run dashboard
```

This builds the dashboard (production build, only when its code changed), starts the backend, and
opens **http://127.0.0.1:8080**. Ctrl+C stops everything. `npm run dashboard:rebuild` forces a fresh
build. Nothing is reachable from other devices on your network.

## 2. One-time setup (Settings)

| Tab | What to set |
|---|---|
| **API keys** | Deriv **app ID** and **token** (Trade scope only, never Payments); TypeSafe key for Jev; optional FMP/FRED for the news filter; Telegram or Discord for phone alerts. Press **Test** on each. |
| **Risk** | Limits the backend enforces before every order: risk per trade, max stake, daily loss, open trades, orders per day. The **Real-money lock** card lives here too. |
| **Deriv** | Account check and the measured contract costs per symbol ("calibration"). |
| **Judges** | Jev (hosted) and Laya (local) status and a test call. |
| **System** | Backend address, health, versions. |

Keys are write-only: the page shows *set / not set*, never the value.

Suggested limits for a very small account: risk per trade **1**, max stake **5**, daily loss **2**,
open trades **1**, orders per day **2** (USD).

## 3. The pages

**Metrics**: performance (expectancy in R, win rate, profit factor, drawdown, equity), execution
(latency), strategy (rule pass rates), data health (candle coverage, gaps), risk (today versus limits).

**Trading**
- *Engine*: symbols, mode (**paper / demo / real**), evaluation (**code / jev / laya / ensemble**),
  risk per trade, strategy version. **▶ Start / ■ Stop**.
- *Signals*: every setup the engine judged, with each rule's result. Accepted signals show
  **Place it on Deriv** (stake, multiplier, take-profit and stop-loss amounts), the confidence JSON,
  and a **Pine Script** you can paste into TradingView to see the same trade on a chart.
- *Positions*: open trades. *Journal*: closed trades by mode, CSV export.

**Strategy**: the decision graph (every rule and where in the ICT videos it comes from), the rule
table, **Backtests** (run one, open its report) and the saved versions.

**Research**: placeholder in the dashboard; transcript tools run from the command line (section 7).

Always visible in the header: feed status, engine state, the **DEMO** / **⚠ REAL** badge, and
**⏻ KILL**, which stops the engine and blocks every order instantly. Releasing it needs two clicks.

## 4. Modes, from safest to live

| Mode | What happens | Needs |
|---|---|---|
| **paper** | Simulated fills on live prices; nothing sent to Deriv. Alerts still fire, so you can trade by hand. | nothing |
| **demo** | Real orders on your Deriv **demo** account. | app ID + token |
| **real** | Real orders on your real account. | 1. `COMMUNITY_ALLOW_REAL_TRADING=true` typed into `.env` by you, then a restart; 2. **Arm real trading** in Settings → Risk with the phrase `I ACCEPT REAL MONEY RISK` (cleared on every restart); 3. Deriv confirming the account is real. Every order still passes the limits. |

**Disarm** or **KILL** stops real trading at once. The app can never switch the `.env` setting itself.

### Automatic trading on Capital.com (the Desk page)

Capital.com is the execution venue that TradingView can display. TradingView has no order API, so
the system places orders through Capital.com's API; when you log in to the same Capital.com account in
TradingView's **Trading Panel**, every order and position appears on your chart.

1. **Settings → API keys → Capital**: API key, login email and the API key's password. Create the key
   on the Capital.com **demo** account (2FA required).
2. **Desk → Connect demo** to verify the keys.
3. **Trading → Engine**: mode **demo**, broker **capital.com**, then **▶ Start**. Accepted setups
   become *proposals*; each one is decided once and kept with its evidence.
4. **Desk → master switch**:

| Master | What happens |
|---|---|
| **OFF** | No automatic orders. Switching to OFF also cancels working orders and closes automated positions (two clicks when anything is open). |
| **PAUSE** | No new orders; whatever is open keeps its broker-side stop and target. |
| **ON** | Accepted setups are placed as broker-side limit orders with stop and target, sized so a stop-out loses your risk amount (the spread counted as risk too), inside every limit. |

Each open trade has its own **OFF** button (cancel the order, or close the position). Click any decision
to open its dossier: the plan, every rule check, the sizing, the exact request sent to the broker and
its reply, and the result. The master switch never bypasses the real-money lock or the kill switch.
Signals are still computed on Deriv's price feed, so each plan is shifted by the price gap between the
two feeds, and refused when that gap exceeds `max_basis_r` (a quarter of the risk by default).

## 5. A trading day (South African time)

The strategy only trades the **New York morning window: 13:00 to 16:00 SAST** (14:00 to 17:00 after
the US clocks change on 1 November), at most one trade a day. A setup that hasn't reached its entry by
**17:30 SAST** (11:30 New York) is cancelled.

1. **Before 13:00**: open the desk, check the feed is live and *Data health* has no gaps, confirm the
   risk limits, then **▶ Start** the engine (paper or demo).
2. **During the window**: alerts arrive in two steps, *Setup armed* (get ready: entry price and
   expiry) and *Entry reached* (the order goes in, or you place it by hand from the Deriv ticket).
   Leave the trade alone once it's on: the stop loss and take profit close it.
3. **After 16:00**: read the Journal, then replay the day to see every setup and why it was or
   wasn't taken:

   ```bash
   cd fx-scalper-community/backend
   ../.venv/Scripts/python.exe -m app.research.day_replay            # today
   ../.venv/Scripts/python.exe -m app.research.day_replay --date 2026-09-25 --mode jev
   ```

   Its *If taken anyway* column shows what each setup would have made with every rule ignored, which
   tells you whether a rule is keeping you out of losers or out of winners.

Forex is closed from Friday about 23:00 until Monday 02:00 SAST; the engine simply sees no setups.

## 6. Backtests: reading the report

Run one from **Strategy → Backtests** (symbol, days, mode). Missing history is downloaded first, and
real Deriv costs are applied. In the report:

- **Expectancy (R)**: average result per trade in units of risk. +0.2R means that, on average, each
  trade risking $1 made 20 cents. This is the number that decides profitability.
- **95% range**: where the true expectancy probably lies. If it includes zero, the strategy isn't
  proven profitable yet, whatever the total says.
- **Out-of-sample**: the last 30% of the period, which a strategy tweak wasn't tuned on. Trust this
  part more than the in-sample result.
- **Execution funnel / node pass rates**: how many setups each rule removed. A rule that removes
  everything means the strategy never trades.
- **What-if overrides**: test a rule change without editing the strategy, for example
  `{"execution": {"target": {"mode": "fixed_r", "r": 2.0}}}`.

Rule of thumb before real money: **at least 50 trades, positive out-of-sample expectancy, and a 95%
range that stays above zero**, then the same behaviour on demo.

## 7. Research tools (command line, from `fx-scalper-community/backend`)

| Task | Command |
|---|---|
| Pull ICT transcripts (resumes; default is the 2022 Mentorship playlist) | `../.venv/Scripts/python.exe -m app.cli transcripts pull` |
| Tag transcripts with Jev and rebuild the concept digest | `../.venv/Scripts/python.exe -m app.cli strategy tag` |
| Download candles | `../.venv/Scripts/python.exe -m app.cli candles backfill frxEURUSD --days 365` |
| Backtest from the terminal | `../.venv/Scripts/python.exe -m app.cli backtest run frxEURUSD --days 180` |
| Replay a day | `../.venv/Scripts/python.exe -m app.research.day_replay` |
| Compare rule variants over a year | `../.venv/Scripts/python.exe -m app.research.rule_variants frxEURUSD` |
| Judge calibration study (does Jev/Laya add edge?) | `../.venv/Scripts/python.exe -m app.research.jev_calibration --symbols frxEURUSD --days 365 --judges jev` |

Transcripts and the digest stay in `backend/data/transcripts/` and are never committed (ICT's content).

## 8. What the evidence says so far

- **Judge study (2026-09-26, 120 NDX setups):** no proven edge; the base setup averaged −0.04R. Jev's
  displacement judgement helped a little, Laya added nothing. Details: `docs/research/`.
- **One-year rule variants (2026-09-28):** the v3 rules produced **zero trades** on EURUSD and
  GBPUSD and one (a loss) on gold. Looser variants trade 24 to 36 times a year and **lose** 0.06R to
  0.36R per trade after costs; the strict ones trade too rarely to measure. Details:
  `docs/research/2026-09-28-rule-variants.md`, reproduce with `python -m app.research.rule_variants`.
- **Today's replay (2026-09-28):** 20 setups across EURUSD, GBPUSD and gold; no trade with code
  judging (one gold loss, −1.08R, with Jev judging). Taken regardless of the rules, gold's five
  window setups would have lost about 4.1R and the forex ones made about +0.8R.

So: use **paper** and **demo** until a version of the strategy clears the rule of thumb in section 6.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `Port 8080 is in use` | A dev server (`npm run dev`) or an earlier desk is still running. Close it, or set `FXS_PORT`. |
| Deriv test says *Invalid application* | The app ID must be your own from developers.deriv.com, and the token must be created after that app. |
| No setups / empty charts | Weekend, or history not downloaded yet; run a backtest or `candles backfill`. |
| Real tab greyed out | Expected until the `.env` unlock and the typed arm are both done. |
| Nothing happens on a button | Hard-refresh the page (Ctrl+F5); the header shows if the backend is unreachable. |
