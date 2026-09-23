"""Tag transcript windows with ICT concepts using Jev, and build the per-concept digest.

One `system_one` call per ~75 s window, with all questions asked in parallel
(speculative fan-out, as the TypeSafe skill recommends):
  concept            Choice: primary ICT concept taught in the passage
  is_rule            Noul: the passage states a checkable trading rule
  gives_execution    Noul: it specifies an entry trigger, stop or target
  specificity        Score: vague → fully mechanical
  bias               Choice: bullish / bearish / both / n.a.

Jev only judges; code chooses the windows, stores the answers, ranks the
passages, and writes the digest. Claude then turns the digest into the rule
schema (strategies/*.json).
"""
from __future__ import annotations

import asyncio
from typing import Callable

from app import config, db
from app.services.jev.client import JevClient
from app.services.transcripts.extract import video_url

WINDOW_S = 75.0
CONTEXT_CHARS = 450  # neighbouring text so a window is not judged out of context
CONCURRENCY = 8      # well inside Jev's 1,200 requests/min

ICT_CONCEPTS: dict[str, str] = {
    "buy_sell_side_liquidity": "Resting liquidity: buy stops above old highs (buy-side) or sell stops below old lows "
                               "(sell-side), including equal highs/lows as targets or pools",
    "liquidity_sweep": "A run, raid, sweep or stop hunt through an old high or low that takes the stops and then "
                       "reverses",
    "market_structure_shift": "Market structure shift or break in structure: price breaks a recent short-term swing "
                              "high or low, signalling a change of direction",
    "displacement": "Displacement: an energetic move with large-bodied candles that leaves imbalances behind",
    "fair_value_gap": "Fair value gap (FVG, imbalance): a three-candle gap where candle 1 and candle 3 wicks do not "
                      "overlap; entering at the gap or its 50% consequent encroachment",
    "order_block": "Order block: the last opposing candle(s) before a displacement move, used as an entry zone",
    "breaker_mitigation": "Breaker, mitigation, rejection or propulsion blocks",
    "ote_fibonacci": "Optimal trade entry: Fibonacci retracement of the swing (62%, 70.5%, 79%) as the entry area",
    "premium_discount": "Premium versus discount: the dealing range's 50% equilibrium; buy in discount, sell in "
                        "premium",
    "time_killzones": "Time of day: killzones (London, New York AM/PM), session opens, macro times, Silver Bullet "
                      "hour windows",
    "htf_bias_draw": "Higher-timeframe bias and draw on liquidity: daily/weekly direction, where price is likely to "
                     "go next, previous day/week high or low",
    "smt_divergence": "SMT divergence: correlated markets (ES vs NQ, EURUSD vs GBPUSD, DXY) failing to confirm each "
                      "other's highs or lows",
    "power_of_three": "Power of three / AMD: accumulation, manipulation (Judas swing) and distribution within a "
                      "daily or session range",
    "model_overview": "Summary of the complete trading model: the step-by-step sequence that combines several "
                      "concepts into one setup",
    "entry_exit_management": "Trade execution specifics: the exact entry trigger, stop-loss placement, profit "
                             "targets, partials and managing the open trade",
    "news_macro_events": "Economic news releases (CPI, NFP, FOMC) and how to avoid or handle them",
    "risk_psychology": "Risk management, position sizing, psychology, discipline, journaling or mindset",
    "market_review_narrative": "Reviewing what price did on a chart without teaching a general, reusable rule",
    "off_topic": "Greetings, housekeeping, personal stories, jokes or chatter unrelated to trading method",
}

QUESTIONS: dict[str, dict] = {
    "concept": {
        "type": "choice",
        "instructions": "Which trading concept is the speaker primarily teaching in `passage`? Use "
                        "`previous_context` and `next_context` only to understand the passage.",
        "criteria": ICT_CONCEPTS,
    },
    "is_rule": {
        "type": "noul",
        "instructions": "`passage` states a trading rule or condition a trader could check on a chart (what to look "
                        "for, when to act, where to enter, exit or place a stop), not only narration or motivation.",
        "criteria": {"true": "A checkable rule or condition is stated.",
                     "false": "Only narration, opinion, review, motivation or chatter."},
    },
    "gives_execution": {
        "type": "noul",
        "instructions": "`passage` specifies at least one of: an entry trigger or entry price location, where the "
                        "stop loss goes, or where the profit target is.",
    },
    "specificity": {
        "type": "score",
        "instructions": "How precisely could the trading idea in `passage` be turned into code?",
        "criteria": [
            "Vague or motivational: nothing testable is described.",
            "A concept is described, but not precisely enough to code.",
            "Mostly precise: clear conditions with some undefined words such as 'strong' or 'clean'.",
            "Fully mechanical: exact conditions, levels, times or sequence that could be coded directly.",
        ],
    },
    "bias": {
        "type": "choice",
        "instructions": "Which trade direction does the rule or example in `passage` apply to?",
        "criteria": {"bullish": "Long setups / buying", "bearish": "Short setups / selling",
                     "both": "Applies symmetrically to longs and shorts",
                     "not_applicable": "No direction is involved"},
    },
}


def windows(segments: list[dict], window_s: float = WINDOW_S) -> list[dict]:
    out: list[dict] = []
    current: dict | None = None
    for seg in segments:
        if current is None or seg["start"] - current["start"] >= window_s:
            if current:
                out.append(current)
            current = {"start": seg["start"], "end": seg["start"] + seg["duration"], "parts": []}
        current["parts"].append(seg["text"])
        current["end"] = seg["start"] + seg["duration"]
    if current:
        out.append(current)
    for i, w in enumerate(out):
        w["idx"], w["text"] = i, " ".join(w.pop("parts"))
    return out


def window_state(title: str, wins: list[dict], i: int) -> dict:
    prev_text = wins[i - 1]["text"][-CONTEXT_CHARS:] if i > 0 else ""
    next_text = wins[i + 1]["text"][:CONTEXT_CHARS] if i + 1 < len(wins) else ""
    return {"source": "ICT (Inner Circle Trader) mentorship video, automatic captions (may contain transcription "
                      "errors such as 'fair value gap' heard as 'fair value cap')",
            "video_title": title, "previous_context": prev_text, "passage": wins[i]["text"], "next_context": next_text}


def _store(video_id: str, win: dict, result) -> None:
    a = result.answers
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO segment_tags(video_id, window_idx, start, end, text, concept, concept_confidence, "
            "concept_probs, is_rule, specificity, bias, bias_confidence, model, tagged_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (video_id, win["idx"], win["start"], win["end"], win["text"], a["concept"]["choice"],
             a["concept"]["confidence"], db.dumps({**a["concept"]["probabilities"],
                                                   "_execution": a["gives_execution"]["noul"]}),
             a["is_rule"]["noul"], a["specificity"]["score_norm"], a["bias"]["choice"], a["bias"]["confidence"],
             result.model, db.utc_now()))


async def tag_videos(video_ids: list[str] | None = None, *, limit: int | None = None, retag: bool = False,
                     progress: Callable[[dict], None] | None = None) -> dict:
    jev = JevClient()
    with db.connect() as conn:
        sql = "SELECT video_id, title FROM videos WHERE status = 'ok'"
        params: tuple = ()
        if video_ids:
            sql += f" AND video_id IN ({','.join('?' * len(video_ids))})"
            params = tuple(video_ids)
        videos = db.rows(conn, sql + " ORDER BY title", params)[: limit or None]
        done_keys = set() if retag else {(r["video_id"], r["window_idx"]) for r in
                                         db.rows(conn, "SELECT video_id, window_idx FROM segment_tags")}
    stats = {"videos": len(videos), "windows": 0, "tagged": 0, "cached": 0, "skipped": 0, "errors": 0,
             "input_tokens": 0, "model": jev.model}
    sem = asyncio.Semaphore(CONCURRENCY)
    async with jev.async_sdk() as sdk:
        for video in videos:
            with db.connect() as conn:
                segs = db.rows(conn, "SELECT start, duration, text FROM transcript_segments WHERE video_id = ? "
                                     "ORDER BY idx", (video["video_id"],))
            wins = windows(segs)
            stats["windows"] += len(wins)

            async def one(i: int, video=video, wins=wins) -> None:
                if (video["video_id"], i) in done_keys:
                    stats["skipped"] += 1
                    return
                async with sem:
                    try:
                        result = await jev.aask(window_state(video["title"] or "", wins, i), QUESTIONS, sdk=sdk)
                    except Exception:
                        stats["errors"] += 1
                        return
                await asyncio.to_thread(_store, video["video_id"], wins[i], result)
                stats["cached" if result.cached else "tagged"] += 1
                stats["input_tokens"] += result.input_tokens or 0
                stats["model"] = result.model or stats["model"]

            await asyncio.gather(*(one(i) for i in range(len(wins))))
            if progress:
                progress(dict(stats, current=video["title"]))
    return stats


def ranked_passages(concept: str | None = None, min_rule: float = 0.6, limit: int = 60) -> list[dict]:
    sql = ("SELECT t.*, v.title FROM segment_tags t JOIN videos v USING (video_id) WHERE t.is_rule >= ?")
    params: list = [min_rule]
    if concept:
        sql += " AND t.concept = ?"
        params.append(concept)
    # Rank: rule-likeness × specificity × concept confidence (code-owned weighting).
    sql += " ORDER BY (t.is_rule * (0.25 + t.specificity) * (0.5 + t.concept_confidence)) DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        items = db.rows(conn, sql, tuple(params))
    for item in items:
        probs = db.loads(item.pop("concept_probs"), {})
        item["execution"] = probs.pop("_execution", None)
        item["url"] = video_url(item["video_id"], item["start"])
    return items


def concept_summary() -> list[dict]:
    with db.connect() as conn:
        return db.rows(conn, "SELECT concept, COUNT(*) AS windows, SUM(is_rule >= 0.6) AS rule_windows, "
                             "ROUND(AVG(specificity), 3) AS avg_specificity FROM segment_tags "
                             "GROUP BY concept ORDER BY rule_windows DESC")


def write_digest(per_concept: int = 25, min_rule: float = 0.6) -> dict:
    """Markdown digest of the strongest rule passages per concept, with timestamp links (gitignored)."""
    summary = concept_summary()
    lines = ["# ICT concept digest (Jev-tagged)", "",
             "Ranked by P(rule) × specificity × concept confidence. Links jump to the timestamp.", "",
             "| Concept | Windows | Rule windows | Avg specificity |", "|---|---:|---:|---:|"]
    lines += [f"| {s['concept']} | {s['windows']} | {s['rule_windows']} | {s['avg_specificity']} |" for s in summary]
    count = 0
    for s in summary:
        if s["concept"] in {"off_topic", "market_review_narrative"}:
            continue
        passages = ranked_passages(s["concept"], min_rule, per_concept)
        if not passages:
            continue
        lines += ["", f"## {s['concept']}", f"_{ICT_CONCEPTS.get(s['concept'], '')}_", ""]
        for p in passages:
            m, sec = divmod(int(p["start"]), 60)
            lines.append(f"- [{p['title']} @ {m}:{sec:02d}]({p['url']}) · rule {p['is_rule']:.2f} · "
                         f"spec {p['specificity']:.2f} · exec {p['execution'] or 0:.2f} · {p['bias']}")
            lines.append(f"  > {p['text']}")
            count += 1
    path = config.transcripts_dir() / "digest_by_concept.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "passages": count, "concepts": summary}
