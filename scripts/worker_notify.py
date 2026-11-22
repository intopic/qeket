"""Send operator and customer notices to their configured destinations."""
from http.client import HTTPSConnection, HTTPException
from ipaddress import IPv6Address, ip_address, ip_network
import json
import os
import re
import socket
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


PUBLIC_V6 = ip_network("2000::/3")

CUSTOMER_NOTICES = {"ProposalCreated", "VoteSubmitted", "ReplayFailed", "CandidateRejected",
                    "UpgradeExecuted", "EmergencyOverrideTriggered"}


def public_addresses(host, port):
    """Every address the host resolves to, IPv4 first. Refuse the host if any is not public.

    Fly Machines reach the internet over IPv4 only, while dual-stack hosts often
    resolve to IPv6 first.
    """
    addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses:
        raise ValueError("customer webhook DNS produced no addresses")
    for address in addresses:
        ip = ip_address(address)
        ip = getattr(ip, "ipv4_mapped", None) or ip
        tunnel = isinstance(ip, IPv6Address) and (ip not in PUBLIC_V6 or ip.sixtofour or ip.teredo)
        if not ip.is_global or ip.is_multicast or ip.is_reserved or tunnel:
            raise ValueError("customer webhook DNS includes a non-public address")
    return sorted(addresses, key=lambda address: ip_address(address).version)


def is_slack_webhook(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc == "hooks.slack.com" and parts.path.startswith("/services/")


def slack_body(event, message):
    # Plain-text blocks, so external values are never read as Slack markup.
    return {"text": event, "blocks": [{"type": "section", "text": {"type": "plain_text", "text": message[:2800]}}]}


def post_customer(url, message):
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment or "%" in parts.hostname):
        raise ValueError("invalid customer webhook URL")
    notice = message.encode()
    connection = HTTPSConnection(parts.hostname, parts.port or 443, timeout=3)
    # Keep TLS certificate validation and SNI at the configured hostname.
    # Connect only to the already validated numeric addresses, so no second DNS
    # resolution (including through a proxy) can redirect the connection.
    addresses = public_addresses(parts.hostname, connection.port)

    def connect(_, timeout, source):
        failure = None
        for address in addresses:
            try:
                return socket.create_connection((address, connection.port), timeout, source)
            except OSError as exc:
                failure = exc
        raise failure

    connection._create_connection = connect
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        connection.request("POST", path, notice, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        # A direct connection never follows redirects.
        if not 200 <= response.status < 300:
            raise OSError("customer webhook refused the notice")
    finally:
        connection.close()


def notify(event, details, customer_url=None):
    """Best-effort delivery. A chat outage must never decide proposal outcome."""
    message = json.dumps({"event": event, **details}, ensure_ascii=False)
    if customer_url and event in CUSTOMER_NOTICES:
        try:
            # A Slack Incoming Webhook refuses a body without "text".
            body = json.dumps(slack_body(event, message)) if is_slack_webhook(customer_url) else message
            post_customer(customer_url, body)
        except (HTTPException, OSError, ValueError):
            print(json.dumps({"event": "notice_delivery_failed", "sink": "customer", "notice": event}), flush=True)
    slack = os.environ.get("CANARY_SLACK_WEBHOOK")
    telegram = os.environ.get("CANARY_TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("CANARY_TELEGRAM_CHAT_ID")
    sinks = []
    if slack and is_slack_webhook(slack):
        sinks.append(("slack", slack, slack_body(event, message)))
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
