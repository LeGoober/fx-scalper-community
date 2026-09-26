"""
notify.py — Send trade notifications to Discord & Telegram (was backend/webhooks.py).

Used by the trading engine to broadcast trade events. Both channels
can run simultaneously; configure via environment variables.
"""
import os
from datetime import datetime, timezone

import httpx as requests  # same .post(url, json=, timeout=) surface as the original


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_ts() -> str:
    return _utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')


def send_discord(message: str, embed: dict = None) -> bool:
    """Send a plain message or embedded message to a Discord webhook."""
    url = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
    if not url:
        return False
    payload = {"content": message[:2000]}
    if embed:
        payload["embeds"] = [embed]
    try:
        r = requests.post(url, json=payload, timeout=5)
        return r.status_code in (200, 204)
    except Exception:
        return False


def send_telegram(message: str) -> bool:
    """Send a text message via a Telegram bot."""
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": message[:4096],
            "parse_mode": "HTML",
        }, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def ticket_lines(ticket: dict | None) -> str:
    """Deriv Trader fields for placing a plan by hand."""
    if not ticket:
        return ""
    cur = ticket.get("currency", "USD")
    lines = [f"Deriv: {ticket['contract']} {ticket['direction']}"]
    if ticket.get("multiplier"):
        lines.append(f"Stake {ticket['stake']:.2f} {cur} · x{ticket['multiplier']}")
    lines.append(f"Take profit {ticket['take_profit']:.2f} {cur} · Stop loss {ticket['stop_loss']:.2f} {cur}")
    lines.append(f"Chart: entry {ticket['entry']:.5f} · stop {ticket['stop']:.5f} · target {ticket['target']:.5f} "
                 f"({ticket['rr']:.1f}R)")
    if ticket.get("note"):
        lines.append(f"Note: {ticket['note']}")
    return "\n".join(lines)


def notify_setup(symbol: str, ticket: dict, expires_at: int):
    """A setup passed every rule and is waiting for price to reach its entry."""
    until = datetime.fromtimestamp(expires_at, timezone.utc).strftime('%H:%M UTC')
    arrow = "🟢" if ticket.get("direction") == "Up" else "🔴"
    msg = (f"{arrow} <b>Setup armed: {symbol}</b>\nWait for price to reach {ticket['entry']:.5f} (until {until}).\n"
           f"{ticket_lines(ticket)}\nNot reached by then: skip it.")
    send_discord(msg.replace("<b>", "**").replace("</b>", "**"))
    send_telegram(msg)


def notify_trade_opened(symbol: str, direction: str, stake: float, price: float, regime: str,
                        ticket: dict | None = None):
    """Broadcast a trade-open event to all configured channels."""
    emoji = "🟢" if direction == "CALL" else "🔴"
    msg = (
        f"{emoji} <b>Trade Opened</b>\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"Stake: ${stake:.2f}\n"
        f"Entry: {price:.5f}\n"
        f"Regime: {regime}\n"
        f"Time: {_fmt_ts()}"
    )
    discord_embed = {
        "title": f"{emoji} Trade Opened: {symbol}",
        "color": 0x00ff00 if direction == "CALL" else 0xff0000,
        "fields": [
            {"name": "Direction", "value": direction, "inline": True},
            {"name": "Stake", "value": f"${stake:.2f}", "inline": True},
            {"name": "Entry", "value": f"{price:.5f}", "inline": True},
            {"name": "Regime", "value": regime, "inline": True},
        ],
        "timestamp": _utcnow().isoformat(),
    }
    if ticket:
        discord_embed["description"] = "Entry reached: place it now.\n" + ticket_lines(ticket)
    send_discord(msg, discord_embed)
    send_telegram(f"<b>{emoji} Trade Opened</b>\n{symbol} {direction} @ {price:.5f}\n"
                  + (f"Entry reached: place it now.\n{ticket_lines(ticket)}" if ticket else f"Stake ${stake:.2f}"))


def notify_trade_closed(symbol: str, direction: str, profit: float, reason: str):
    """Broadcast a trade-close event to all configured channels."""
    emoji = "💰" if profit >= 0 else "💸"
    msg = (
        f"{emoji} <b>Trade Closed</b>\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"P&L: ${profit:.2f}\n"
        f"Reason: {reason}\n"
        f"Time: {_fmt_ts()}"
    )
    color = 0x00ff00 if profit >= 0 else 0xff0000
    discord_embed = {
        "title": f"{emoji} Trade Closed: {symbol}",
        "color": color,
        "fields": [
            {"name": "Direction", "value": direction, "inline": True},
            {"name": "P&L", "value": f"${profit:+.2f}", "inline": True},
            {"name": "Reason", "value": reason, "inline": False},
        ],
        "timestamp": _utcnow().isoformat(),
    }
    send_discord(msg, discord_embed)
    send_telegram(
        f"<b>{emoji} Trade Closed</b>\n{symbol} {direction} | "
        f"{'✅' if profit >= 0 else '❌'} ${profit:+.2f} — {reason}"
    )


def notify_error(symbol: str, error: str):
    """Broadcast an error event to all configured channels."""
    msg = (
        f"⚠️ <b>Trade Error</b>\n"
        f"Symbol: {symbol}\n"
        f"Error: {error}\n"
        f"Time: {_fmt_ts()}"
    )
    send_discord(msg)
    send_telegram(msg)
