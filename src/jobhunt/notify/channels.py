"""Push channels. Each sends one message to the user (never to anyone else) or raises."""

from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Protocol

from jobhunt.models.config import NotificationsConfig, SmtpConfig

TIMEOUT = 15


class NotifyError(Exception):
    pass


def http_post(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST and return the status. Replaced in tests."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return int(resp.status)
    except Exception as e:  # urllib raises HTTPError, URLError, timeouts...
        raise NotifyError(f"{type(e).__name__}: {e}") from e


class Channel(Protocol):
    name: str

    def send(self, title: str, text: str, *, urgent: bool = False) -> None: ...


class Ntfy:
    name = "ntfy"

    def __init__(self, topic: str, user_agent: str) -> None:
        self.url = (
            topic if topic.startswith(("https://", "http://")) else f"https://ntfy.sh/{topic}"
        )
        self.user_agent = user_agent

    def send(self, title: str, text: str, *, urgent: bool = False) -> None:
        headers = {
            "Title": title.encode("ascii", "replace").decode(),
            "Priority": "high" if urgent else "default",
            "Tags": "briefcase",
            "User-Agent": self.user_agent,
        }
        status = http_post(self.url, text.encode("utf-8")[:4000], headers)
        if status >= 300:
            raise NotifyError(f"ntfy answered HTTP {status}")


class Discord:
    name = "discord"

    def __init__(self, webhook: str, user_agent: str) -> None:
        self.url = webhook
        self.user_agent = user_agent

    def send(self, title: str, text: str, *, urgent: bool = False) -> None:
        content = f"**{title}**\n{text}"[:1990]
        body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
        status = http_post(
            self.url, body, {"Content-Type": "application/json", "User-Agent": self.user_agent}
        )
        if status >= 300:
            raise NotifyError(f"Discord answered HTTP {status}")


class Smtp:
    name = "smtp"

    def __init__(self, cfg: SmtpConfig) -> None:
        self.cfg = cfg

    def send(self, title: str, text: str, *, urgent: bool = False) -> None:
        c = self.cfg
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = c.username or c.to
        msg["To"] = c.to  # only ever the user's own address
        msg.set_content(text)
        password = os.environ.get(c.password_env) if c.password_env else None
        if c.password_env and not password:
            raise NotifyError(f"environment variable {c.password_env} is not set")
        try:
            with smtplib.SMTP(c.host, c.port, timeout=TIMEOUT) as s:
                if c.starttls:
                    s.starttls()
                if c.username and password:
                    s.login(c.username, password)
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise NotifyError(f"{type(e).__name__}: {e}") from e


def configured(cfg: NotificationsConfig, user_agent: str) -> list[Channel]:
    out: list[Channel] = []
    if cfg.ntfy_topic:
        out.append(Ntfy(cfg.ntfy_topic, user_agent))
    if cfg.discord_webhook:
        out.append(Discord(cfg.discord_webhook, user_agent))
    if cfg.smtp:
        out.append(Smtp(cfg.smtp))
    return out
