"""Structured JSON logging with nanosecond timestamps (spec §10: no print)."""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

import orjson


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts_ns": time.time_ns(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if extra:
            doc["ctx"] = extra
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc"] = self.formatException(record.exc_info)
        return orjson.dumps(doc).decode()


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def ctx(logger: logging.Logger, level: int, msg: str, **kv: Any) -> None:
    """Log with structured context fields."""
    logger.log(level, msg, extra={"ctx": kv})
