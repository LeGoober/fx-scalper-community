// Placeholder boot (replaced by the Kimi-generated UI). It proves the wiring end to end:
// FastAPI serves ui/dist, api.ts reaches the backend, and the stream connects.
import { connectStream, health, risk, trading } from "./api.js";

class FxsApp extends HTMLElement {
  async connectedCallback() {
    this.innerHTML = `<main style="font:13px system-ui;padding:24px;max-width:720px">
      <h1 style="font-size:18px">FX Scalper · ICT × Jev</h1>
      <p>Backend wiring check. The real UI is generated from <code>docs/UI_BRIEF.md</code>.</p>
      <pre id="out" style="font:12px ui-monospace;background:#0001;padding:12px;border-radius:6px">loading…</pre>
      <p>Stream: <b id="stream">connecting…</b> · last event: <span id="last">—</span></p></main>`;
    const out = this.querySelector("#out")!;
    try {
      const [h, r, e] = await Promise.all([health(), risk.get(), trading.engine()]);
      out.textContent = JSON.stringify({ health: h, account: r.real_trading.effective ? "REAL" : "DEMO",
        kill_switch: r.kill_switch, engine_running: e.running }, null, 2);
    } catch (err) {
      out.textContent = String(err);
    }
    connectStream((m) => { this.querySelector("#last")!.textContent = `${m.type} ${m.message ?? ""}`; },
                  (s) => { this.querySelector("#stream")!.textContent = s; });
  }
}
customElements.define("fxs-app", FxsApp);
