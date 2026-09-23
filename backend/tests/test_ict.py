"""Phase 3/4: ICT detectors, scanner causality, strategy schema, backtest maths, Jev plumbing."""
import asyncio
import random
from datetime import datetime, timezone

import pytest

from app.services.backtest import engine, metrics
from app.services.ict import features as F
from app.services.ict import strategy as S
from app.services.ict.scanner import ScanParams, Scanner


def bars_from(rows, start=1_773_000_000, step=300):
    return F.Bars([start + i * step for i in range(len(rows))], [r[0] for r in rows], [r[1] for r in rows],
                  [r[2] for r in rows], [r[3] for r in rows], step)


# A textbook long: swing high 99.8, swing low 98.0, raid to 97.5, displacement breaks 99.8, FVG [99.0, 100.9].
TEXTBOOK = [
    (100, 100.5, 99.5, 100), (100, 100.6, 99.6, 100.2), (100.2, 100.8, 99.8, 100.4), (100.4, 101, 100, 100.6),
    (100.6, 102, 100.4, 101.5), (101.5, 101.6, 100.5, 100.8), (100.8, 101, 99.8, 100), (100, 100.2, 99, 99.2),
    (99.2, 99.4, 98, 98.2), (98.2, 99.5, 98.1, 99.3), (99.3, 99.8, 98.6, 99), (99, 99.2, 98.3, 98.5),
    (98.5, 98.6, 97.5, 97.8), (97.8, 99, 97.7, 98.9), (98.9, 101, 98.8, 100.8), (100.8, 102.6, 100.9, 102.4),
    (102.4, 102.5, 101.9, 102.1), (102.1, 102.2, 101.4, 101.6),
]


def test_fvg_detection():
    b = bars_from([(1, 2, 0.5, 1.5), (1.5, 3.5, 1.4, 3.4), (3.4, 4, 2.5, 3.8)])
    gap = F.fvg_at(b, 2)
    assert gap and gap.direction == "bullish" and gap.bottom == 2 and gap.top == 2.5 and gap.ce == 2.25
    b = bars_from([(4, 4.2, 3, 3.1), (3.1, 3.2, 1, 1.1), (1.1, 2.4, 0.8, 1)])
    assert F.fvg_at(b, 2).direction == "bearish"
    assert F.fvg_at(bars_from([(1, 2, 0.5, 1.5), (1.5, 2.2, 1.4, 2), (2, 2.4, 1.9, 2.3)]), 2) is None


def test_swings_confirm_only_after_k_bars():
    b = bars_from(TEXTBOOK)
    lows = {s.index: s for s in F.swings(b, 2) if s.kind == "low"}
    assert lows[8].price == 98.0 and lows[8].confirmed_at == 10


@pytest.mark.parametrize("utc,zone", [
    (datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc), "ny_am"),   # 07:30 EST
    (datetime(2026, 7, 15, 11, 30, tzinfo=timezone.utc), "ny_am"),   # 07:30 EDT (DST)
    (datetime(2026, 7, 15, 7, 30, tzinfo=timezone.utc), "london"),   # 03:30 EDT
    (datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc), None),       # 13:00 EDT, not a listed AM zone
])
def test_killzones_are_new_york_time_with_dst(utc, zone):
    assert F.killzone_of(int(utc.timestamp()), ["london", "ny_am"]) == zone


def test_scanner_finds_the_textbook_long():
    setups = Scanner(bars_from(TEXTBOOK), ScanParams(use_session_levels=False, displacement_min_body_atr=1.2)).run()
    longs = [s for s in setups if s.direction == "long"]
    assert len(longs) == 1
    s = longs[0]
    assert (s.sweep_index, s.sweep_level, s.mss_index, s.mss_level) == (12, 98.0, 14, 99.8)
    assert (s.fvg.index, s.fvg.bottom, s.fvg.top) == (15, 99.0, 100.9)
    assert s.armed_at == 15 and s.sweep_extreme == 97.5


def _random_walk(n=3000, seed=11):
    rng = random.Random(seed)
    rows, price = [], 100.0
    for _ in range(n):
        o = price
        c = o + rng.gauss(0, 0.35) + (1.4 if rng.random() < 0.02 else 0) * rng.choice((-1, 1))
        rows.append((o, max(o, c) + abs(rng.gauss(0, 0.15)), min(o, c) - abs(rng.gauss(0, 0.15)), c))
        price = c
    return rows


def _key(s):
    return (s.direction, s.sweep_index, s.mss_index, s.fvg.index, s.fvg.top, s.fvg.bottom, s.armed_at,
            s.sweep_extreme, s.leg_high, s.leg_low)


def test_no_look_ahead_in_scanner_and_levels():
    rows, cut = _random_walk(), 1800
    base = Scanner(bars_from(rows, step=300)).run()
    rng = random.Random(99)
    mutated = rows[:cut] + [(o + rng.uniform(-3, 3), h + 5, lo - 5, c + rng.uniform(-3, 3))
                            for o, h, lo, c in rows[cut:]]
    after = Scanner(bars_from(mutated, step=300)).run()
    before_cut = [_key(s) for s in base if s.armed_at < cut]
    assert before_cut, "random walk should produce some setups"
    assert before_cut == [_key(s) for s in after if s.armed_at < cut]


def test_strategy_v1_schema_validates_and_diagrams():
    schema = S.load_file(S.config.STRATEGIES_DIR / "ict_2022_model.v1.json")
    assert schema.id == "ict_2022_model" and schema.version == 1
    for node in schema.nodes:
        if node.kind == "code":
            assert node.detector in S.DETECTORS, node.id
        if node.kind == "jev":
            assert node.question and node.question["type"] in {"choice", "score", "noul"}
            assert node.fallback is None or node.fallback in S.DETECTORS
    text = S.mermaid(schema)
    assert all(n.id in text for n in schema.nodes) and text.startswith("flowchart TD")
    g = S.graph(schema)
    assert len(g["edges"]) == len(schema.nodes) + 3


def test_simulation_is_conservative_when_bar_hits_stop_and_target():
    rows = [(100, 100.2, 99.8, 100)] * 5 + [(100, 100.1, 98.9, 99.0), (99.0, 103.0, 97.0, 101.0)]
    b = bars_from(rows)
    setup = F.Setup("long", 0, 99.5, "swing_low", 98.0, 2, 100, F.FVG(3, "bullish", 99.2, 98.8), 100.5, 98.0,
                    1.5, 1, 4)
    plan = S.Plan("long", entry=99.0, stop=98.0, target=102.0, risk=1.0, rr=3.0, target_name="t")
    ex = S.ExecutionSpec()
    trade = engine._multiplier(setup, plan, b, ex, cost=0.0, exit_hhmm=None)
    assert trade["exit_reason"] == "stop" and trade["r"] == -1.0


def test_expectancy_matches_bootcamp_example():
    s = metrics.summarise([2, -1, 2, -1, -1, 2, -1, -1, 2, -1])
    assert s["expectancy_r"] == 0.2 and s["win_rate"] == 0.4 and s["payoff"] == 2.0
    assert s["breakeven_win_rate"] == round(1 / 3, 4) and s["expectancy_ci95"][0] < 0.2 < s["expectancy_ci95"][1]


def test_jev_answers_are_normalised_and_cached(monkeypatch):
    from app.services.jev import client as J

    class A:  # minimal stand-ins for SDK answer objects
        def __init__(self, **kw):
            self.__dict__.update(kw)

    calls = {"n": 0}

    class FakeResponse:
        model = "jev-test-1"
        usage = A(input_tokens=321)
        answers = {"q_choice": A(type="choice", choice="a", confidence=0.8, probabilities={"a": 0.9, "b": 0.1}),
                   "q_score": A(type="score", score=2.0, confidence=0.7, probabilities={0: 0.1, 1: 0.2, 2: 0.7}),
                   "q_noul": A(type="noul", noul=0.66)}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def system_one(self, state, questions):
            calls["n"] += 1
            return FakeResponse()

    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", FakeClient)
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-test")
    qs = {"q_choice": {"type": "choice", "instructions": "x", "criteria": {"a": "A", "b": "B"}},
          "q_score": {"type": "score", "instructions": "y", "criteria": ["0", "1", "2"]},
          "q_noul": {"type": "noul", "instructions": "z"}}
    first = J.JevClient("jev-test").ask({"s": 1}, qs)
    assert first.answers["q_score"]["score_norm"] == 1.0 and first.answers["q_noul"]["noul"] == 0.66
    assert first.input_tokens == 321 and not first.cached
    second = J.JevClient("jev-test").ask({"s": 1}, qs)
    assert second.cached and second.answers == first.answers and calls["n"] == 1


def test_runtime_jev_gates(monkeypatch):
    schema = S.load_file(S.config.STRATEGIES_DIR / "ict_2022_model.v1.json")
    node = next(n for n in schema.nodes if n.id == "htf_draw")
    ok, value, _ = S._gate_jev(node, {"type": "choice", "choice": "buy_side_above", "confidence": 0.6,
                                      "probabilities": {"buy_side_above": 0.7, "sell_side_below": 0.2, "unclear": 0.1}},
                               "long")
    assert ok and value == "buy_side_above"
    ok, _, _ = S._gate_jev(node, {"type": "choice", "choice": "buy_side_above", "confidence": 0.6,
                                  "probabilities": {"buy_side_above": 0.7, "sell_side_below": 0.2}}, "short")
    assert not ok


def test_backtest_api_rejects_jev_mode_without_key(client):
    r = client.post("/api/backtests/run", json={"mode": "jev"})
    assert r.status_code == 412


def test_legacy_backtest_button_still_works(client):
    r = client.post("/api/backtests/run")
    assert r.status_code == 200 and r.json()["summary"]["scenario_passes"] == 3


def test_async_engine_end_to_end_on_synthetic_data(monkeypatch):
    from app.services.deriv import history
    rows = _random_walk(4000, seed=5)
    candles = [{"epoch": 1_773_000_000 + i * 60, "open": o, "high": h, "low": lo, "close": c}
               for i, (o, h, lo, c) in enumerate(rows)]
    history.upsert("TEST", 60, candles)
    res = asyncio.run(engine.run(engine.BacktestParams("ict_2022_model", "TEST", 1_773_000_000,
                                                       1_773_000_000 + 4000 * 60, mode="code")))
    r = res["results"]["code"]
    assert res["setups"] >= 1 and r["execution_funnel"]["setups"] == res["setups"]
    assert r["summary"]["trades"] == len(r["trades"])
