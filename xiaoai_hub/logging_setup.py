"""日志初始化：控制台 + 可选轮转文件。

用法::

    from xiaoai_hub import logging_setup
    logging_setup.setup("INFO", "/var/log/xiaoai-hub.log")
    log = logging_setup.get_logger(__name__)
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _ColorFormatter(logging.Formatter):
    """控制台着色（非 TTY 时自动退化为纯文本）。"""

    COLORS = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[1;38;5;196m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool) -> None:
        super().__init__(_FORMAT, _DATEFMT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{text}{self.RESET}" if color else text


def setup(level: str = "INFO", log_file: Optional[str] = None,
          force: bool = False) -> logging.Logger:
    """初始化根 logger（幂等）。"""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED and not force:
        return root

    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_ColorFormatter(use_color=sys.stderr.isatty()))
    root.addHandler(stream)

    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
            root.addHandler(file_handler)
        except OSError as exc:  # 日志文件不可写不应阻断启动
            root.warning("日志文件不可用 %s: %s", log_file, exc)

    # 第三方库降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
