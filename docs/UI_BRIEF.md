# FX Scalper — UI brief for Kimi

> Paste this whole document into Kimi together with `ui/src/api.ts` and `docs/openapi.json`.
> One generation pass should produce a working UI. Everything below is derived from the real backend.

---

## 0. What you are building

A **personal trading console** for one user. It runs an ICT (Inner Circle Trader) strategy pipeline:

1. YouTube transcripts are pulled.
2. The Jev AI model tags them by trading concept.
3. They become a versioned **strategy graph** of typed decision nodes.
4. The graph is backtested on Deriv candles.
5. It then runs live in **paper** or **Deriv demo** mode.

The UI is a **structured, tabular control surface** over a FastAPI backend at `/api` (same origin), plus one WebSocket at `/api/stream`. It is a dense tool, not a marketing site. Think "flight deck with good typography".

## 1. Hard constraints (non-negotiable)

| Rule | Detail |
|---|---|
| Language | TypeScript, compiled by `tsc` to native ES modules. **No framework** (no React/Vue/Svelte/Lit), **no bundler**, no CSS framework, no icon font. |
| Components | Native **Custom Elements** (`class X extends HTMLElement`, `customElements.define("fxs-…")`). Light DOM is fine; if you use Shadow DOM, pass theme tokens through CSS custom properties. |
| Data | Call the backend **only** through the functions exported by `ui/src/api.ts`. Don't write raw `fetch` in components, and don't change `api.ts` signatures. Live updates **only** via `connectStream()` from `api.ts`. |
| Runtime deps | Exactly one: `lightweight-charts` (TradingView's library), for candle and equity charts. Import it as `import { createChart } from "lightweight-charts"`; `index.html` has an import map pointing to `./vendor/lightweight-charts.mjs`. Everything else is hand-written (SVG for the strategy graph, `<table>` for tables). |
| Routing | Hash router (`#/metrics`, `#/trading`, …), about 50 lines, in `src/router.ts`. |
| Size | Everything except `vendor/` must be under **60 KB gzipped**. |
| Secrets | API keys are **write-only**. Never display, store (localStorage/sessionStorage) or log a key value. The backend only ever returns `is_set`. |
| Accessibility | Keyboard-reachable controls, visible focus rings, `aria-live="polite"` for toasts, colour never the only signal (paired with text/icons). |

## 2. Files

**You receive:**
- `ui/src/api.ts`: the typed contract. Read it first; its comments list every stream event.
- `docs/openapi.json`: the full REST schema.
- `ui/index.html`: shell with the import map and `<fxs-app>`. You may restyle it but keep the import map and the `dist/main.js` module script.

**You produce (all under `ui/`):**
```
src/main.ts               boot: theme, router, stream, <fxs-app>
src/router.ts             hash router
src/state.ts              tiny store: latest engine status, risk, jobs, toasts (EventTarget-based)
src/format.ts             number/time formatting helpers (section 8)
src/components/           one file per custom element (fxs-status-bar, fxs-table, fxs-stat, fxs-chart-equity,
                          fxs-chart-candles, fxs-strategy-graph, fxs-job-tray, fxs-toast, fxs-key-field, …)
src/pages/                metrics.ts, trading.ts, strategy.ts, research.ts, settings.ts
styles/tokens.css         design tokens (light + dark)
styles/app.css            layout + components
```

## 3. Design direction

**Mood:** calm, precise, a bit of craft. Light and dark are equal citizens.

- **Theme:**
  - Follow `prefers-color-scheme`, with a manual toggle (auto / light / dark) stored in `localStorage` under `fxs.theme`. That's the only localStorage use.
  - All colours are tokens on `:root` and `[data-theme="dark"]`.
- **Gradients, used deliberately, 3–4 places only:**
  1. A thin header band. Suggested: deep teal → indigo in dark; soft mint → periwinkle in light.
  2. The top-left accent edge of metric cards.
  3. The equity-curve area fill, fading to transparent.
  4. The DEMO/REAL badge.

  Everything else is flat surfaces with 1px hairline borders.
- **Typography:**
  - System UI stack for text; `ui-monospace` for numbers in tables.
  - Always `font-variant-numeric: tabular-nums`.
  - Sizes: 12 px (table), 13 px (body), 15 px (section title), 22 px (hero number).
- **Density:** 32 px table rows, 8 px grid, cards with 16 px padding. Tables are the primary visual; sections are clustered in **tabbed or segmented panels**, not endless scroll.
- **Semantic colours:**
  - `--pos` green for gains/pass, `--neg` red for losses/fail, `--warn` amber for flags, `--info` blue for neutral info.
  - Jev nodes violet (`--jev`), code nodes blue (`--code`), trigger nodes amber (`--trig`), to match the backend's Mermaid palette.
- **Avoid (AI-slop tells):**
  - purple-to-pink glow everywhere, glassmorphism stacks, neon shadows;
  - emoji in headings, giant hero sections, stock illustrations;
  - "✨"/"🚀" copy, rounded-everything blobs.
  
  Aim for a Bloomberg-terminal clarity with modern type.
- **Creative latitude:** micro-interactions (a 150 ms row-flash when a value updates via the stream, a subtle pulse on the live-feed dot), a well-drawn strategy graph, and a tasteful empty state per table.

## 4. App shell

```
┌ header band (gradient) ─────────────────────────────────────────────────────────────┐
│ FX Scalper · ICT×Jev   [● feed 287 ms] [engine: PAPER running] [DEMO]  [⏻ KILL]  ◐    │
├───────────┬─────────────────────────────────────────────────────────────────────────┤
│ Metrics   │  page title · segmented tabs                                             │
│ Trading   │  ┌ card ┐ ┌ card ┐ ┌ card ┐                                             │
│ Strategy  │  ┌ table ─────────────────────────────────────────────┐                  │
│ Research  │  └────────────────────────────────────────────────────┘                  │
│ Settings  │                                                         [jobs tray ▲ 2]  │
└───────────┴─────────────────────────────────────────────────────────────────────────┘
```

- **Status bar (always visible)** — `fxs-status-bar`. On load it reads `trading.engine()` and `risk.get()`, then stays updated from the stream.
  - Feed dot + `feed_rtt_ms`.
  - Engine state: `running`, `config.mode`, `config.evaluation`.
  - **Account badge:** a large **DEMO** badge unless `risk.real_trading.effective === true`, in which case **REAL** in red with a gradient border. For demo engine mode, also show `broker.loginid`.
  - **Kill switch.** One click calls `risk.killSwitch(true)` with no confirm dialog; speed matters. When active the button turns into "Release" (`risk.killSwitch(false)`, with a confirm) and the header band turns red.
- **Jobs tray** (bottom right). Lists running jobs from `jobs.list()` plus `stream.job.progress` updates. Each shows a progress bar (`progress.done / progress.total`, or `windows`), status, and a cancel button (`jobs.cancel`).
- **Toasts.** Show `level: "warning" | "error"` stream events, plus `trade.opened`, `trade.closed`, `signal.new` with `status: "accepted"`, and `risk.blocked`. Auto-dismiss after 6 s; errors stay until dismissed.

## 5. Global behaviours

- **Stream:**
  - Call `connectStream(onMessage, onState)` once in `main.ts` and dispatch into `state.ts`.
  - Pages subscribe to the store; they never open their own WebSocket.
  - On `closed`, grey out the feed dot and show "reconnecting…". `api.ts` already retries with backoff.
- **Loading / empty / error states** for every table and card:
  - loading = skeleton rows;
  - empty = one-line explanation plus the action that fills it (e.g. "No candles yet. Backfill 90 days").
  - An `ApiError` shows its `message`, which is the backend's `detail`/`error`, inline in the card. HTTP 412 means "missing key"; link to Settings → API keys.
- **Refresh policy:** fetch on page enter; refresh after the user's own actions; otherwise rely on stream events. Poll only `trading.engine()` every 5 s while the Trading page is open *and* the engine is running.
- **Long numbers:** prices use the symbol's precision (`pip_size` from `market.symbols()`; default 5 dp for forex, 2 dp for indices/gold).

## 6. Pages (each section names the api.ts calls it uses)

### 6.1 Metrics — `#/metrics` (default page)
One call feeds the page: `metrics.overview()`. It refreshes on `trade.closed`, `backtest.done` and `job.done`.

| Cluster (segmented tab) | Content | Fields |
|---|---|---|
| **Performance** | Mode switch paper / demo / real; hero stats row (Expectancy R, Win rate, Profit factor, Max DD R, Trades, P&L); equity chart (lightweight-charts area series on `balance`, with `r` on a second scale) | `performance.modes[mode].summary`, `.equity`, `.pnl`. Show `expectancy_ci95` as "±" text under the hero number, and `p_expectancy_le_0` as "P(edge ≤ 0)". |
| **Execution** | Deriv feed round-trip, Jev avg/max latency, calls, entry slippage in R | `execution.*` |
| **Strategy** | Live signal funnel: accepted vs rejected counts; node pass-rate table (node id, evaluated, passed, pass-rate bar); killzone distribution | `strategy.signals`, `.nodes`, `.killzones` |
| **Data health** | Candle coverage table (symbol, granularity, bars, first → last), transcripts (videos ok/problems/segments), Jev tags, calendar events | `data.*`; "Backfill" button per row → `market.backfill({symbol, granularity, days: 30})` |
| **Risk** | Today: realised P&L, loss vs `max_daily_loss` (progress bar), orders vs `max_orders_per_day`, open vs `max_concurrent`; kill switch state; real-money lock status (3 checks: env allows / armed / effective) | `risk.*` |

A **"Last backtest"** card shows `last_backtest.results[mode]` and links to `#/strategy/backtests/{id}`.

### 6.2 Trading — `#/trading`
Tabs: **Engine · Signals · Positions · Journal**

- **Engine**
  - Config form fields:
    - symbols: multi-select from `market.symbols()`. Show `ict_suitable: false` symbols disabled, with the tooltip "synthetic index: ICT premise does not apply". Pre-select `ict_defaults`.
    - mode: paper | demo;
    - evaluation: code | jev (jev is disabled when `strategy.jevUsage().key_set` is false);
    - risk_amount in account currency;
    - strategy/version picker from `strategy.schemas()`.
  - Start → `trading.start(cfg)`; Stop → `trading.stop()`.
  - Errors: HTTP 403 is the risk lock (e.g. the account is not verified as demo); show the message prominently. HTTP 409 means already running.
  - Live symbol table from `trading.engine().symbols`: symbol, last price (flash on change), last tick age, bars, scans, evaluations, signals, pending entries (direction · entry · stop · target · expires), position, errors / last_error.
  - A **contract profile** strip per symbol from `trading.profiles()`: accepted multipliers, commission (`commission_rate` as bps), Rise/Fall payout and its breakeven win rate, and `executable` badges. Button "Recalibrate" → `trading.calibrate()`.
- **Signals.** `trading.signals({limit:200})` table: time, symbol, direction, status (accepted/rejected), killzone, plan (entry/stop/target, rr), flags. Expanding a row shows the **node-by-node trace** (`decision.nodes`: id, passed ✓/✗, value, detail, source code/jev/fallback). Stream `signal.new` prepends rows.
- **Positions.** Open trades (`trading.trades({status:"open"})`) plus engine `position` objects: live R estimate from `last_price`.
- **Journal.** `trading.trades()` table with mode filter; columns: opened, closed, mode, symbol, side, entry, exit, stop, target, R (coloured), P&L, reason (`meta.exit_reason`). A "Download CSV" link uses `trading.tradesCsvUrl(mode)`.

### 6.3 Strategy — `#/strategy`
Tabs: **Graph · Nodes · Backtests · Versions**

- **Graph.** `strategy.graph(id, version)` → draw `fxs-strategy-graph` as **native SVG**, top-down, one column, with rounded rectangles:
  - trigger nodes amber, code nodes blue, Jev nodes violet hexagons;
  - plan / risk / order nodes neutral;
  - dashed "fail → no trade" edges for `on_fail === "reject"`; flag nodes get an amber corner tag.
  
  Clicking a node opens a side panel with: description, gate, the Jev question (`question.type`, instructions, criteria rendered as a small table), and **citations** as links (`https://www.youtube.com/watch?v={video_id}&t={t}s`, label "Episode title @m:ss", note in italics). A "Mermaid source" link uses `strategy.mermaidUrl`.
- **Nodes.** `strategy.schema(id)` table: #, label, kind, detector/question type, params/gate (compact JSON), on_fail, citations count.
- **Backtests:**
  - **Run form:** symbol, days (default 365), mode code | jev | compare, contract override (multiplier | rise_fall + minutes), entry override (fvg_ce | fvg_edge). Overrides map to `overrides` exactly as documented in `BacktestRequest`, e.g. `{execution:{contract:{type:"rise_fall",rise_fall_minutes:15}}}`.
  - Submit → `backtests.run(req)` gives a Job; progress arrives in the jobs tray; `backtest.done` refreshes the list.
  - **Runs table:** `backtests.list()`: created, symbol, mode, trades, win %, E[R], PF, max DD.
  - **Run detail** (`#/strategy/backtests/{id}` → `backtests.get(id)`):
    1. `warnings` as an amber banner. It says when a contract isn't offered on Deriv for that symbol.
    2. Summary stats row, with CI and P(edge ≤ 0).
    3. Equity chart; in compare mode, `code` and `jev` as two series.
    4. **Execution funnel** as a horizontal bar sequence: setups → passed filters → filled (plus not filled / missed / capped).
    5. **Node pass rates** table.
    6. In-sample vs out-of-sample side by side, plus walk-forward folds.
    7. Breakdowns by killzone / direction / exit reason.
    8. Trades table (from `trades[mode]`).
- **Versions.** `strategy.schemas()` list with status (draft/review/approved) and changelog (`schema.changelog`).

### 6.4 Research — `#/research`
Tabs: **Transcripts · Search · Jev tagging**

- **Transcripts:**
  - Source card from `transcripts.sources()`: default playlist, plus free fallback exporters as external links.
  - "Preview" → `transcripts.preview(url)` shows the video list with `stored` status. "Pull" → `transcripts.pull({url, delay_s})`, with progress in the jobs tray.
  - Videos table: `transcripts.videos()`: title, status (ok/failed/blocked badge), segments, tagged windows, fetched at.
  - If `totals.blocked > 0`, show a banner: "YouTube is rate-limiting this IP. Retry later, or import from a fallback exporter".
  - Import dialog → `transcripts.import({video_id, format, content})`, with file picker and paste box.
  - The video reader (`transcripts.video(id)`) lists segments with timestamps that link to YouTube.
- **Search.** `transcripts.search(q)` shows hits with highlighted terms and timestamp links.
- **Jev tagging:**
  - Button "Tag with Jev" → `strategy.tag()`. Disabled with a hint if the key isn't set; HTTP 412 means the key is missing.
  - Concept table from `strategy.concepts()`: concept, description, windows, rule windows, avg specificity.
  - Clicking a concept → `strategy.tags({concept})` shows ranked passages: P(rule), specificity, execution, bias, passage text, timestamp link.
  - Usage card: `strategy.jevUsage()` (calls, tokens, estimated $ cost, avg latency, configured model).

### 6.5 Settings — `#/settings`
Tabs: **API keys · Deriv · OpenBB · TradingView · Jev · Risk · System**

- **API keys:**
  - `secrets.list()`, grouped by `group`. Each row is a `fxs-key-field`: label, help text, status pill (Set / Not set), a password input that is **empty by default**, and Save → `secrets.set(name, value)` (then clear the input).
  - Also per row: Clear → `secrets.clear(name)` (with confirm), and **Test** → `secrets.test(name)`, which shows ok/fail, latency and a short detail.
  - Non-secret config (`secret: false`) may show its `value`.
- **Deriv:**
  - Account from `trading.engine().broker`, or a Test button → `secrets.test("deriv_token")`: loginid, `is_virtual`, `endpoint` (demo/real), balance, RTT.
  - Token guidance text: "Create at app.deriv.com → API token with **Read + Trade** scopes only. Never Payments/Admin."
  - Contract profiles (same strip as the Trading page).
- **OpenBB.** `market.openbbStatus()`: installed/version/loaded, keyed providers, calendar provider. The calendar preview (`market.calendar()`) lists upcoming high-impact events (currency, title, time in NY and local). There's also a daily-close cross-check per symbol (`market.crosscheck`).
- **TradingView:**
  - Webhook URL (`{origin}/api/webhooks/tradingview`), with a note that TradingView needs a public tunnel and a paid plan.
  - "Rotate secret" → `secrets.rotateTradingView()`. Show the returned value **once**, in a copy-able field, with the warning "copy it into your Pine script input now".
  - Alert log from `eventsLog(50, "webhook.")`.
  - A link to `tradingview/ict_2022_model.pine` (repo file).
- **Jev.** Configured model (`jevUsage().configured_model`), with the note "pin a version such as jev-1.13.0 in .env for reproducible backtests". Also "Test Jev" → `strategy.jevTest()` and usage stats.
- **Risk:**
  - Limits form → `risk.setLimits()`.
  - Real-money lock panel: three read-only checks (`env_allows`, `armed`, `effective`).
    - If `env_allows` is false: show the text "Real trading can only be enabled by editing COMMUNITY_ALLOW_REAL_TRADING in .env by hand," and no controls.
    - If true: an "Arm real trading" dialog that requires typing `REAL_MONEY_PHRASE` exactly → `risk.armReal(phrase)`; and Disarm → `risk.disarmReal()`.
- **System.** Theme toggle; event log (`eventsLog(200)`) filterable by kind prefix; jobs history (`jobs.list()`); API health (`health()`); links to `/docs` (Swagger).

## 7. Action → endpoint map (every button)

| Action | Call | Confirm? | Side effect |
|---|---|---|---|
| Kill switch | `risk.killSwitch(true)` | **No** | Stops engine, blocks all orders |
| Release kill switch | `risk.killSwitch(false)` | Yes | Orders allowed again |
| Start engine | `trading.start(cfg)` | Yes if mode = demo | Streams candles; demo places Deriv demo orders |
| Stop engine | `trading.stop()` | No | Cancels pending entries |
| Recalibrate costs | `trading.calibrate()` | No | Refreshes contract profiles |
| Backfill candles | `market.backfill(...)` | No | Background job |
| Pull transcripts | `transcripts.pull(...)` | No | Background job |
| Import transcript | `transcripts.import(...)` | No | Stores segments |
| Tag with Jev | `strategy.tag()` | Yes (shows est. cost) | Background job, spends Jev tokens |
| Run backtest | `backtests.run(req)` | No | Background job |
| Save key | `secrets.set(name, value)` | No | Writes .env |
| Clear key | `secrets.clear(name)` | Yes | Removes from .env |
| Test key | `secrets.test(name)` | No | One live call |
| Rotate TV secret | `secrets.rotateTradingView()` | Yes | Old alerts stop authenticating |
| Save limits | `risk.setLimits(...)` | No | Applies immediately |
| Arm real | `risk.armReal(phrase)` | Typed phrase | Only if .env allows |

## 8. Formatting

- **R values:** sign, plus 2 dp (`+1.36R`, `−1.00R`), coloured by sign. Win rate as a percentage with 0–1 dp.
- **Money:** currency plus 2 dp.
- **bps:** 1 dp.
- **Times:** tables show *local time* with a tooltip in *New York time*. Killzones are always NY. Epoch fields are seconds.
- **Empty values:** `—`.
- **Null expectancy:** "n/a (no trades)".

## 9. Acceptance checklist

- [ ] All five pages render with real data from a running backend (`python -m app`), in light and dark themes.
- [ ] No component calls `fetch` directly; everything goes through `api.ts`.
- [ ] Keys never appear in the DOM after saving, in storage, or in console logs.
- [ ] The DEMO/REAL badge and kill switch are visible on every page and at every viewport width ≥ 1024 px.
- [ ] Stream events update the status bar, jobs tray, signals table and journal without a page reload.
- [ ] Every table has loading, empty and error states.
- [ ] The strategy graph renders from `/graph` with correct node colours, and its citations link to YouTube timestamps.
- [ ] A backtest can be launched, followed in the jobs tray, and opened to the detail view (funnel, nodes, IS/OOS, equity).
- [ ] `npm run build` succeeds, and the gzipped size of `dist/` excluding `vendor/` is under 60 KB.

## 10. Build and serve

```
cd ui && npm install && npm run build      # tsc → dist/, then copies index.html, styles and vendor
cd ../backend && python -m app             # FastAPI serves ui/dist at http://127.0.0.1:5001/
```

For development, `npm run watch` keeps compiling; reload the browser (the backend serves `dist/`).
