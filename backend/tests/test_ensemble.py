"""Ensemble confidence JSON and Pine generation."""
import asyncio

import pytest

from app.services import pine
from app.services.ict import ensemble
from app.services.ict import strategy as S
from app.services.jev.client import JevResult


class FakeJudge:
    """Answers every question with fixed values; records how often it was asked."""

    def __init__(self, name, noul=0.8, score_norm=0.8, choice_p=0.8, fail=False):
        self.model, self.noul, self.score_norm, self.choice_p, self.fail = name, noul, score_norm, choice_p, fail
        self.calls = 0
        self.available = True

    async def aask(self, state, questions, **_):
        self.calls += 1
        if self.fail:
            raise RuntimeError("judge down")
        out = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                out[qid] = {"type": "noul", "noul": self.noul}
            elif q["type"] == "score":
                out[qid] = {"type": "score", "score": 2.4, "score_norm": self.score_norm, "confidence": 0.7,
                            "probabilities": {}}
            else:
                keys = list(q["criteria"])
                probs = {k: (self.choice_p if i == 0 else (1 - self.choice_p) / (len(keys) - 1))
                         for i, k in enumerate(keys)}
                out[qid] = {"type": "choice", "choice": keys[0], "confidence": 0.6, "probabilities": probs}
        return JevResult(out, self.model, 100, 5.0)


def _v3_judge_nodes():
    schema = S.load_file(S.config.STRATEGIES_DIR / "ict_2022_model.v3.json")
    return schema, [n for n in schema.nodes if n.kind == "jev"]


def test_every_schema_goes_to_every_judge_and_report_is_complete():
    schema, nodes = _v3_judge_nodes()
    jev, laya = FakeJudge("jev-test"), FakeJudge("laya-test")
    rep = asyncio.run(ensemble.validate({"x": 1}, nodes, "long", {"jev": jev, "laya": laya},
                                        weights={"jev": 0.6, "laya": 0.4}, threshold=0.6,
                                        plan={"entry": 1.1, "stop": 1.09, "target": 1.12}))
    k = len(ensemble.schemas_for(nodes))
    assert k == 3 and jev.calls == k and laya.calls == k and len(rep["validations"]) == 2 * k
    assert rep["kind"] == "fxs.confidence/v1" and rep["decision"] == "execute"
    assert rep["nodes"]["displacement_quality"]["mean"] == pytest.approx(0.8)
    assert rep["nodes"]["htf_draw"]["n"] == 2 * k


def test_disagreement_lowers_confidence_and_can_block():
    _, nodes = _v3_judge_nodes()
    judges = {"jev": FakeJudge("j", 0.95, 0.95, 0.95), "laya": FakeJudge("l", 0.1, 0.1, 0.1)}
    rep = asyncio.run(ensemble.validate({}, nodes, "long", judges, weights={"jev": 0.5, "laya": 0.5}, threshold=0.6))
    assert rep["agreement"] < 0.5 and rep["decision"] == "skip"
    assert any("below gate" in r or "confidence" in r for r in rep["reasons"])


def test_a_failing_judge_is_recorded_not_fatal():
    _, nodes = _v3_judge_nodes()
    judges = {"jev": FakeJudge("j"), "laya": FakeJudge("l", fail=True)}
    rep = asyncio.run(ensemble.validate({}, nodes, "long", judges))
    errors = [v for v in rep["validations"] if v["error"]]
    assert errors and all(v["judge"] == "laya" for v in errors) and rep["decision"] == "execute"


def test_failed_code_node_forces_skip():
    _, nodes = _v3_judge_nodes()
    rep = asyncio.run(ensemble.validate({}, nodes, "long", {"jev": FakeJudge("j")},
                                        code_results=[{"id": "killzone", "passed": False}]))
    assert rep["decision"] == "skip" and "code node failed: killzone" in rep["reasons"]


def test_choice_value_uses_the_direction_specific_label():
    _, nodes = _v3_judge_nodes()
    htf = next(n for n in nodes if n.id == "htf_draw")
    ans = {"type": "choice", "choice": "buy_side_above", "confidence": 0.5,
           "probabilities": {"buy_side_above": 0.7, "sell_side_below": 0.2, "unclear": 0.1}}
    assert ensemble.value_of(ans, htf, "long") == 0.7 and ensemble.value_of(ans, htf, "short") == 0.2


def test_pine_script_is_valid_shape_and_sized_by_risk():
    src = pine.pine_for_plan(symbol="frxEURUSD", direction="long", entry=1.1000, stop=1.0980, target=1.1050,
                             risk_amount=2.0, signal_time=1_790_000_000, signal_id="sig-x", confidence=0.71)
    assert src.startswith("//@version=6") and "strategy(" in src and "strategy.exit" in src
    assert "limit = entryPrice" in src and "stop = stopPrice" in src and "strategy.long" in src
    assert "size 1000 units" in src and "R:R 2.50" in src
    with pytest.raises(ValueError):
        pine.pine_for_plan(symbol="frxEURUSD", direction="long", entry=1.1, stop=1.12, target=1.13,
                           risk_amount=1, signal_time=0)


def test_pine_endpoint(client):
    r = client.post("/api/trading/pine", json={"symbol": "OTC_NDX", "direction": "short", "entry": 20000,
                                               "stop": 20040, "target": 19900, "risk_amount": 1})
    assert r.status_code == 200 and "strategy.short" in r.text
    assert client.post("/api/trading/pine", json={"symbol": "x", "direction": "short", "entry": 1, "stop": 0.5,
                                                  "target": 0.2}).status_code == 422


def test_laya_client_normalises_and_batches_with_cache(monkeypatch):
    from app.services import laya_client as L

    class FakeAgent:
        def __init__(self):
            self.batches = 0

        def _one(self, i):
            return {"answers": {"b": {"type": "choice", "choice": "x", "probabilities": {"x": 0.7, "y": 0.3},
                                      "confidence": 0.4},
                                "s": {"type": "score", "score": 2.0, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
                                      "confidence": 0.5},
                                "n": {"type": "noul", "noul": 0.25 + i / 100}},
                    "usage": {"input_tokens": 50}}

        def predict(self, state, questions):
            return self._one(0)

        def predict_batch(self, states, questions, batch_size=16):
            self.batches += 1
            return [self._one(i) for i in range(len(states))]

    agent = FakeAgent()
    monkeypatch.setattr(L, "_load", lambda: agent)
    qs = {"b": {"type": "choice", "instructions": "?", "criteria": {"x": "X", "y": "Y"}},
          "s": {"type": "score", "instructions": "?", "criteria": ["a", "b", "c"]},
          "n": {"type": "noul", "instructions": "?"}}
    client = L.LayaClient()
    first = client.ask_batch([{"i": 1}, {"i": 2}, {"i": 3}], qs)
    assert agent.batches == 1 and len(first) == 3 and not first[0].cached
    assert first[0].answers["s"]["score_norm"] == 1.0 and first[1].answers["n"]["noul"] == 0.26
    again = client.ask_batch([{"i": 1}, {"i": 2}, {"i": 3}], qs)
    assert agent.batches == 1 and all(r.cached for r in again)          # served from cache
    single = asyncio.run(client.aask({"i": 2}, qs))
    assert single.cached and single.answers == first[1].answers         # same cache as the batch path
