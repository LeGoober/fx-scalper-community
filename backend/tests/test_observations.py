"""Phase 2: point-in-time observations, scheduler jobs, TradingView alerts as gated signal sources."""
import asyncio
import time

import pytest

from app import scheduler
from app.services import execution, risk
from app.services.data import observations as obs
from test_execution import FakeBroker


def test_identical_content_is_stored_once():
    assert obs.record("news:x", "news", {"title": "Fed holds"}, key="u1") is not None
    assert obs.record("news:x", "news", {"title": "Fed holds"}, key="u1") is None
    assert len(obs.recent("news")) == 1


def test_snapshot_never_sees_what_arrived_later():
    t0 = 1_790_000_000
    obs.record("news:x", "news", {"title": "early"}, key="a", published_at=t0 - 60, ingested_at=t0 - 30)
    obs.record("news:x", "news", {"title": "late"}, key="b", published_at=t0 - 60, ingested_at=t0 + 600)  # arrived late
    obs.record("news:x", "news", {"title": "future"}, key="c", published_at=t0 + 60, ingested_at=t0 + 60)
    seen = [o["payload"]["title"] for o in obs.snapshot(t0, kinds=["news"])]
    assert seen == ["early"]   # published before t0 but ingested after it is NOT visible at t0


def test_revisions_are_kept_and_the_snapshot_returns_the_version_known_then():
    t0 = 1_790_000_000
    key = "1790003600|USD|Non-Farm Payrolls"
    obs.record("calendar:ff", "calendar", {"title": "NFP", "actual": None}, key=key, ingested_at=t0)
    obs.record("calendar:ff", "calendar", {"title": "NFP", "actual": "250K"}, key=key, ingested_at=t0 + 7200)
    before = obs.snapshot(t0 + 3600, kinds=["calendar"], lookback_s=10 ** 6)
    after = obs.snapshot(t0 + 9000, kinds=["calendar"], lookback_s=10 ** 6)
    assert [o["payload"]["actual"] for o in before] == [None]      # the release was not known yet
    assert [o["payload"]["actual"] for o in after] == ["250K"]     # one row per key: the latest version
    assert after[0]["revision_of"] == before[0]["id"]


def test_currency_filter():
    obs.record("news:x", "news", {"title": "ECB cuts"}, key="e", currencies=["EUR"])
    obs.record("news:x", "news", {"title": "BoJ holds"}, key="j", currencies=["JPY"])
    assert [o["payload"]["title"] for o in obs.snapshot(time.time() + 1, currencies=["EUR"])] == ["ECB cuts"]


def test_headline_currency_tags():
    from app.services.data.openbb_feed import _currencies_in
    assert _currencies_in("EUR/USD slides as Fed signals hike") == ["EUR", "USD"]
    assert _currencies_in("Gold jumps; yen firm") == ["JPY", "XAU"]
    assert _currencies_in("Australian Dollar dips despite hawkish RBA") == ["AUD"]
    assert _currencies_in("US Dollar Index eyes yearly high") == ["USD"]
    assert _currencies_in("EURUSD holds 1.10; GBP/JPY slips") == ["EUR", "GBP", "JPY", "USD"]
    assert _currencies_in("Refund policy update") == []   # 'fund' is not a currency code


def test_a_failing_job_is_recorded_not_fatal():
    def boom():
        raise RuntimeError("provider down")
    job = scheduler.Periodic("t", 60, boom)
    asyncio.run(job.run_once())
    asyncio.run(job.run_once())
    assert job.runs == 2 and job.last_ok is None and "provider down" in job.last_error


def test_disabled_job_does_not_run():
    calls = []
    job = scheduler.Periodic("t", 60, lambda: calls.append(1), enabled=lambda: False)
    asyncio.run(job.run_once())
    assert calls == [] and job.runs == 0


@pytest.fixture
def tv(monkeypatch, client):
    monkeypatch.setenv("COMMUNITY_TV_WEBHOOK_SECRET", "s3cret")
    broker = FakeBroker()
    monkeypatch.setattr(execution, "SERVICE", execution.ExecutionService(factory=lambda n, k: broker))
    risk.set_limits({"max_daily_loss": 50.0})
    yield client, broker
    risk.set_autonomy("off")


def _alert(**kw):
    return {"secret": "s3cret", "ticker": "FX:EURUSD", "event": "my_breakout", "id": "a1", "direction": "long",
            "stop": 1.0970, "target": 1.1030, "entry": 1.0990} | kw


def test_unregistered_alert_is_only_an_observation(tv):
    client, broker = tv
    r = client.post("/api/webhooks/tradingview", json=_alert()).json()
    assert r["observation"] and r["proposal"] is None and broker.placed == []
    assert obs.recent("tv_alert")[0]["payload"]["direction"] == "long"
    assert "secret" not in obs.recent("tv_alert")[0]["payload"]


def test_registered_alert_proposes_and_the_master_switch_still_decides(tv):
    client, broker = tv
    assert client.put("/api/webhooks/tradingview/sources/my_breakout", json={"risk_amount": 1.0}).status_code == 200
    off = client.post("/api/webhooks/tradingview", json=_alert(id="a2")).json()["proposal"]
    assert off["verdict"] == "skipped" and "master switch OFF" in off["reasons"] and broker.placed == []
    risk.set_autonomy("on")
    on = client.post("/api/webhooks/tradingview", json=_alert(id="a3")).json()["proposal"]
    assert on["verdict"] == "executed" and len(broker.placed) == 1 and broker.placed[0].order_type == "limit"
    again = client.post("/api/webhooks/tradingview", json=_alert(id="a3")).json()
    assert again["status"] == "duplicate" and len(broker.placed) == 1   # replayed alert: no second order


def test_registered_alert_without_a_stop_never_trades(tv):
    client, broker = tv
    client.put("/api/webhooks/tradingview/sources/my_breakout", json={})
    risk.set_autonomy("on")
    r = client.post("/api/webhooks/tradingview", json=_alert(id="a4", stop=None)).json()
    assert r["proposal"]["status"] == "ignored" and broker.placed == []


def test_tradingview_sources_are_demo_only(client):
    r = client.put("/api/webhooks/tradingview/sources/x", json={"account_kind": "real"})
    assert r.status_code == 422
