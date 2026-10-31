from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

CONFIG_FILE = Path(__file__).with_name("config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    # 同一个私聊会话里，两次回复之间至少隔几秒
    "private_gap_seconds": 4.0,
    # 同一个群会话里，两次回复之间至少隔几秒
    "group_gap_seconds": 3.0,
    # 在间隔上再加 0~N 秒随机抖动，避免每次都卡得一模一样
    "jitter_seconds": 1.5,
    # 最多让她等多少秒，防止异常状态下卡太久
    "max_wait_seconds": 12,
    # 管理员私聊不限制（她自己跟爸说话不用等）
    "admin_exempt": True,
}


def _load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if not CONFIG_FILE.exists():
        return cfg
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"回复节奏：配置读取失败，使用默认值：{exc}")
        return cfg
    if isinstance(loaded, dict):
        cfg.update(loaded)
    return cfg


def _as_float(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


class Main(star.Star):
    """同一会话连续回复时留出间隔，避免几秒内连发好几条。"""

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = _load_config()
        # umo -> 上一次真正要发出去的时间（monotonic）
        self._last_sent: dict[str, float] = {}
        self._admin_ids: list[str] | None = None

    def _admins(self) -> list[str]:
        if self._admin_ids is not None:
            return self._admin_ids
        try:
            admins = self.context.get_config().get("admins_id", [])
        except Exception:  # noqa: BLE001
            admins = []
        if isinstance(admins, (list, tuple)):
            self._admin_ids = [str(item).strip() for item in admins if str(item).strip()]
        else:
            self._admin_ids = []
        return self._admin_ids

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return False
        return bool(sender) and sender in self._admins()

    @filter.on_decorating_result()
    async def pace_reply(self, event: AstrMessageEvent) -> None:
        if not self.cfg.get("enabled", True):
            return
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        if result is None or not getattr(result, "chain", None):
            return
        # 只给模型回复限速；指令、错误提示之类的系统消息不掺和。
        try:
            if not result.is_model_result():
                return
        except Exception:  # noqa: BLE001
            pass

        try:
            is_private = event.is_private_chat()
        except Exception:  # noqa: BLE001
            is_private = False
        if is_private and self.cfg.get("admin_exempt", True) and self._is_admin(event):
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return

        gap_key = "private_gap_seconds" if is_private else "group_gap_seconds"
        default_gap = DEFAULT_CONFIG[gap_key]
        gap = _as_float(self.cfg.get(gap_key, default_gap), default_gap)
        if gap <= 0:
            return
        jitter = _as_float(
            self.cfg.get("jitter_seconds", 1.5),
            DEFAULT_CONFIG["jitter_seconds"],
        )
        max_wait = _as_float(
            self.cfg.get("max_wait_seconds", 12),
            DEFAULT_CONFIG["max_wait_seconds"],
        )

        now = time.monotonic()
        last = self._last_sent.get(umo)
        wait = 0.0
        if last is not None:
            need = gap + random.uniform(0.0, jitter)
            wait = need - (now - last)
        if wait > max_wait:
            wait = max_wait
        if wait > 0:
            logger.debug("回复节奏：%s 等待 %.1f 秒", umo, wait)
            # 先把这次要占的发送时刻写进去再睡：多条回复是各自独立的事件任务，
            # 如果睡完才写，两个并发任务会读到同一个时间戳、算出同一个 wait，
            # 然后在同一秒一起发出去。提前占位才能真的排成队。
            self._last_sent[umo] = now + wait
            await asyncio.sleep(wait)
        self._last_sent[umo] = time.monotonic()
