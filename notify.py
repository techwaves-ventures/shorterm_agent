"""Notification fan-out: stdout always, webhook + macOS notification if configured."""
import os
import subprocess
import logging
import requests

log = logging.getLogger(__name__)


def notify(title: str, body: str) -> None:
    print(f"\n=== {title} ===\n{body}\n", flush=True)
    log.info("%s | %s", title, body.replace("\n", " | "))

    webhook = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            requests.post(webhook, json={"text": f"*{title}*\n{body}"}, timeout=10)
        except Exception as e:
            log.warning("Webhook failed: %s", e)

    try:
        safe_title = title.replace('"', "'")
        safe_body = body.replace('"', "'")[:300]
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe_body}" with title "{safe_title}"',
            ],
            check=False,
            timeout=5,
        )
    except Exception:
        pass
