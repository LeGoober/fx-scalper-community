"""Command line for the long-running jobs (same code paths as the API).

    python -m app.cli transcripts pull [URL] [--limit N] [--force]
    python -m app.cli candles backfill frxEURUSD --granularity 60 --days 30
    python -m app.cli strategy tag [--video ID] [--limit N]     # needs TYPESAFE_API_KEY
    python -m app.cli strategy diagram
    python -m app.cli backtest run frxEURUSD --days 730 [--mode code|jev|compare]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from app import db


def _print_progress(stats: dict) -> None:
    keys = ("done", "total", "ok", "skipped", "failed")
    line = " ".join(f"{k}={stats.get(k)}" for k in keys if k in stats)
    print("\r" + line, end="", flush=True)


def transcripts_pull(args: argparse.Namespace) -> int:
    from app.services.transcripts import extract
    stats = extract.pull(args.url, limit=args.limit, delay=args.delay, force=args.force, progress=_print_progress)
    print()
    print(json.dumps({k: v for k, v in stats.items() if k != "failures"}, indent=1))
    for failure in stats["failures"]:
        print("  FAILED", failure["video_id"], failure["error"])
    if stats["blocked"]:
        print("YouTube blocked this IP. Try later, from another network, or use a free exporter "
              "(GET /api/transcripts/sources) and POST /api/transcripts/import.")
        return 2
    return 0


def candles_backfill(args: argparse.Namespace) -> int:
    from app.services.deriv import history
    end = int(time.time())
    stats = asyncio.run(history.backfill(args.symbol, args.granularity, int(end - args.days * 86400), end,
                                         progress=lambda p: print(f"\r{p['done']}/{p['windows']} windows, "
                                                                  f"{p['fetched']} candles", end="", flush=True)))
    print()
    print(json.dumps({k: v for k, v in stats.items() if k != "errors"}, indent=1))
    for error in stats["errors"]:
        print("  ERROR", error)
    return 1 if stats["errors"] else 0


def strategy_tag(args: argparse.Namespace) -> int:
    from app.services.jev import tagging
    stats = asyncio.run(tagging.tag_videos(args.video or None, limit=args.limit, retag=args.retag,
                                           progress=lambda s: print(f"\r{s.get('current')}: tagged={s['tagged']} "
                                                                    f"cached={s['cached']} errors={s['errors']}",
                                                                    end="", flush=True)))
    print()
    print(json.dumps(stats, indent=1))
    print(json.dumps(tagging.write_digest(), indent=1, default=str)[:2000])
    return 0


def strategy_diagram(args: argparse.Namespace) -> int:
    from app import config
    from app.services.ict import strategy as S
    for path in sorted(config.STRATEGIES_DIR.glob("*.json")):
        schema = S.load_file(path)
        md = path.with_suffix(".md")
        body = [f"# {schema.name} (v{schema.version}, {schema.status})", "", schema.description, "",
                "```mermaid", S.mermaid(schema), "```", "", "## Nodes", "",
                "| # | Node | Kind | Rule | ICT source |", "|---|---|---|---|---|"]
        for i, n in enumerate(schema.nodes, 1):
            cites = "<br>".join(f"[{c.title or c.video_id} @{c.t // 60}:{c.t % 60:02d}]"
                                f"(https://www.youtube.com/watch?v={c.video_id}&t={c.t}s)" for c in n.citations)
            body.append(f"| {i} | **{n.label}** | {n.kind}{' (' + n.on_fail + ')' if n.on_fail == 'flag' else ''} "
                        f"| {n.description} | {cites or '—'} |")
        md.write_text("\n".join(body) + "\n", encoding="utf-8")
        print("wrote", md)
    return 0


def backtest_run(args: argparse.Namespace) -> int:
    from app.services.backtest import engine
    end = int(time.time())
    params = engine.BacktestParams(args.strategy, args.symbol, int(end - args.days * 86400), end, mode=args.mode,
                                   extra={"overrides": json.loads(args.overrides)} if args.overrides else {})
    result = asyncio.run(engine.run_and_store(params))
    for mode, r in result["results"].items():
        print(f"[{mode}] {result['symbol']} {result['from']} → {result['to']} setups={result['setups']}")
        print("  summary:", json.dumps(r["summary"]))
        print("  funnel: ", json.dumps(r["execution_funnel"]))
        print("  OOS:    ", json.dumps(r["validation"].get("out_of_sample")))
    print("run id:", result["id"])
    return 0


def main(argv: list[str] | None = None) -> int:
    from app.services.transcripts.extract import DEFAULT_PLAYLIST
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="group", required=True)

    tr = sub.add_parser("transcripts").add_subparsers(dest="cmd", required=True)
    pull = tr.add_parser("pull", help="Pull transcripts for a playlist/channel/video")
    pull.add_argument("url", nargs="?", default=DEFAULT_PLAYLIST)
    pull.add_argument("--limit", type=int)
    pull.add_argument("--delay", type=float, default=1.5)
    pull.add_argument("--force", action="store_true")
    pull.set_defaults(fn=transcripts_pull)

    cd = sub.add_parser("candles").add_subparsers(dest="cmd", required=True)
    bf = cd.add_parser("backfill", help="Backfill Deriv candles into the local DB")
    bf.add_argument("symbol")
    bf.add_argument("--granularity", type=int, default=60)
    bf.add_argument("--days", type=float, default=30)
    bf.set_defaults(fn=candles_backfill)

    st = sub.add_parser("strategy").add_subparsers(dest="cmd", required=True)
    tag = st.add_parser("tag", help="Tag stored transcripts with Jev and write the concept digest")
    tag.add_argument("--video", action="append", help="Only these video ids (repeatable)")
    tag.add_argument("--limit", type=int)
    tag.add_argument("--retag", action="store_true")
    tag.set_defaults(fn=strategy_tag)
    dg = st.add_parser("diagram", help="Write strategies/*.md (Mermaid diagram + cited node table)")
    dg.set_defaults(fn=strategy_diagram)

    bt = sub.add_parser("backtest").add_subparsers(dest="cmd", required=True)
    btr = bt.add_parser("run", help="Backtest a strategy on stored candles")
    btr.add_argument("symbol")
    btr.add_argument("--strategy", default="ict_2022_model")
    btr.add_argument("--days", type=float, default=90)
    btr.add_argument("--mode", choices=["code", "jev", "compare"], default="code")
    btr.add_argument("--overrides", help='JSON patch, e.g. \'{"execution": {"entry": "fvg_edge"}}\'')
    btr.set_defaults(fn=backtest_run)

    args = parser.parse_args(argv)
    db.init()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
