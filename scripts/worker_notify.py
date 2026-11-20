"""Send operator notices to explicitly configured Slack or Telegram destinations."""
import json
import os
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def notify(event, details):
    """Best-effort delivery. A chat outage must never decide proposal outcome."""
    message = json.dumps({"event": event, **details}, ensure_ascii=False)
    slack = os.environ.get("CANARY_SLACK_WEBHOOK")
    telegram = os.environ.get("CANARY_TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("CANARY_TELEGRAM_CHAT_ID")
    sinks = []
    if slack:
        try:
            url = urlsplit(slack)
        except ValueError:
            url = urlsplit("")
        if url.scheme == "https" and url.netloc == "hooks.slack.com" and url.path.startswith("/services/"):
            # Use plain-text blocks so external values are not Slack markup.
            sinks.append(("slack", slack, {"text": event, "blocks": [{
                "type": "section", "text": {"type": "plain_text", "text": message[:2800]},
            }]}))
    if telegram and chat and re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", telegram):
        sinks.append(("telegram", f"https://api.telegram.org/bot{telegram}/sendMessage",
                      {"chat_id": chat, "text": message[:3500], "link_preview_options": {"is_disabled": True}}))
    opener = build_opener(NoRedirect)
    for name, url, body in sinks:
        try:
            req = Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with opener.open(req, timeout=3) as response:
                response.read(1024)
        except (OSError, ValueError):
            print(json.dumps({"event": "notice_delivery_failed", "sink": name, "notice": event}), flush=True)
