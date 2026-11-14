"""Minimal AstrBot API logger double for offline tests, never used by plugin code."""

import logging
import sys
import types


def install_logger_stub():
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    if not hasattr(api, "logger"):
        api.logger = logging.getLogger("astrbot")
    return api.logger
