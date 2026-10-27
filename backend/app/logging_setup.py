"""Logging for the API and the worker: job IDs on every line, secrets blanked, optional JSON."""

import json
import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.utils.run_context import JobContextFilter, SecretRedactionFilter


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "job": getattr(record, "job", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    if getattr(root, "_research_configured", False):
        return
    handler = logging.StreamHandler()
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [job %(job)s] %(name)s: %(message)s"))
    handler.addFilter(JobContextFilter())
    handler.addFilter(SecretRedactionFilter(
        [settings.gemini_api_key.get_secret_value(), settings.tavily_api_key.get_secret_value()]
    ))
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Request lines from httpx/the Google SDK add noise and could include query strings.
    for noisy in ("httpx", "httpx2", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._research_configured = True
