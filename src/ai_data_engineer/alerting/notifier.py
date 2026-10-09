"""Where alerts go. Slack (incoming webhook) or the console; email/PagerDuty later implement
the same ``Notifier`` protocol. Uses only the standard library (no new dependency)."""

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from ai_data_engineer.graph.models import DataSource
from ai_data_engineer.ingestion.connections import resolve_connection_url

SLACK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class AlertMessage:
    title: str
    lines: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        return "\n".join([self.title, *self.lines])


class Notifier(Protocol):
    def send(self, message: AlertMessage) -> None: ...


class ConsoleNotifier:
    """Prints alerts; used when a source has no webhook (and in the lab)."""

    def send(self, message: AlertMessage) -> None:
        print(message.as_text())


class SlackNotifier:
    def __init__(self, webhook_url: str) -> None:
        if not webhook_url.startswith("https://"):
            raise ValueError("the Slack webhook URL must start with https://")
        self._url = webhook_url

    def send(self, message: AlertMessage) -> None:
        payload = {"text": slack_text(message)}
        request = urllib.request.Request(  # noqa: S310 - https enforced in __init__
            self._url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=SLACK_TIMEOUT_SECONDS):  # noqa: S310
            pass


def slack_text(message: AlertMessage) -> str:
    """Slack mrkdwn: bold title, one bullet per line."""
    return "\n".join([f"*{message.title}*", *(f"• {line}" for line in message.lines)])


def notifier_for(source: DataSource) -> Notifier:
    """Slack if the source names a webhook variable, otherwise the console."""
    if source.alert_webhook_ref:
        return SlackNotifier(resolve_connection_url(source.alert_webhook_ref))
    return ConsoleNotifier()
