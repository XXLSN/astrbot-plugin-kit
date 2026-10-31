from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

PLUGIN_NAME = "astrbot_plugin_usage_cost"
CONFIG_FILE = Path(__file__).with_name("config.json")

WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "currency_symbol": "¥",
    "prices": {
        "deepseek-flash": {
            "input": 1.0,
            "cached_input": 0.02,
            "output": 4.0,
            "peak_input": 2.0,
            "peak_cached_input": 0.04,
            "peak_output": 8.0,
        }
    },
    "fallback": {
        "input": 1.0,
        "cached_input": 0.02,
        "output": 4.0,
    },
    "peak_hours": "9-12,14-18",
    "peak_weekdays": "mon-fri",
    "holidays": [],
}


def _split_ranges(raw: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
        except ValueError:
            continue
        if 0 <= start < end <= 24:
            ranges.append((start, end))
    return ranges


def _parse_weekdays(raw: str) -> set[int]:
    days: set[int] = set()
    for part in str(raw or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            a, b = WEEKDAYS.get(left), WEEKDAYS.get(right)
            if a is None or b is None:
                continue
            if a <= b:
                days.update(range(a, b + 1))
            else:
                days.update(range(a, 7))
                days.update(range(0, b + 1))
        elif part in WEEKDAYS:
            days.add(WEEKDAYS[part])
    return days or {0, 1, 2, 3, 4}


class Main(star.Star):
    """统计 AstrBot 自己的 token 用量，并按配置单价估算花费。"""

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = self._load_config()
        self._tz = self._load_timezone()
        self._peak_ranges = _split_ranges(
            str(self.cfg.get("peak_hours", DEFAULT_CONFIG["peak_hours"]))
        )
        self._peak_weekdays = _parse_weekdays(
            str(self.cfg.get("peak_weekdays", DEFAULT_CONFIG["peak_weekdays"]))
        )
        self._holidays = {
            str(item).strip()
            for item in (self.cfg.get("holidays") or [])
            if str(item).strip()
        }
        self._prices = self._load_prices()

    # ---------- 配置 ----------

    def _load_config(self) -> dict[str, Any]:
        import json

        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[usage_cost] 读取 config.json 失败：{exc}")
        return cfg

    def _load_timezone(self) -> ZoneInfo:
        name = str(self.cfg.get("timezone") or "Asia/Shanghai")
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning(f"[usage_cost] 未知时区 {name!r}，改用 Asia/Shanghai")
            return ZoneInfo("Asia/Shanghai")

    def _load_prices(self) -> dict[str, dict[str, float]]:
        prices: dict[str, dict[str, float]] = {}
        raw = self.cfg.get("prices") or {}
        if isinstance(raw, dict):
            for model, item in raw.items():
                if not isinstance(item, dict):
                    continue
                prices[str(model)] = {
                    key: float(item.get(key) or 0)
                    for key in (
                        "input",
                        "cached_input",
                        "output",
                        "peak_input",
                        "peak_cached_input",
                        "peak_output",
                    )
                }
        fallback = self.cfg.get("fallback")
        if not isinstance(fallback, dict):
            fallback = DEFAULT_CONFIG["fallback"]
        prices["__fallback__"] = {
            key: float(fallback.get(key) or 0)
            for key in ("input", "cached_input", "output")
        }
        return prices

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        return bool(checker()) if callable(checker) else False

    # ---------- 价格 ----------

    def _is_peak(self, moment_local: datetime) -> bool:
        local_date = moment_local.date().isoformat()
        if local_date in self._holidays:
            return False
        if moment_local.weekday() not in self._peak_weekdays:
            return False
        hour = moment_local.hour
        return any(start <= hour < end for start, end in self._peak_ranges)

    def _rate_for(self, model: str, moment_local: datetime) -> dict[str, float]:
        rate = self._prices.get(model) or self._prices.get("__fallback__", {})
        peak = self._is_peak(moment_local)
        if peak and rate.get("peak_input") is not None:
            return {
                "input": rate.get("peak_input") or rate.get("input") or 0,
                "cached_input": rate.get("peak_cached_input")
                or rate.get("cached_input")
                or 0,
                "output": rate.get("peak_output") or rate.get("output") or 0,
            }
        return {
            "input": rate.get("input") or 0,
            "cached_input": rate.get("cached_input") or 0,
            "output": rate.get("output") or 0,
        }

    # ---------- 数据库 ----------

    def _db_path(self) -> Path | None:
        try:
            from astrbot.core.config.default import DB_PATH

            path = Path(DB_PATH)
            if path.exists():
                return path
        except Exception:  # noqa: BLE001
            pass

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidates = [
            Path(get_astrbot_data_path()) / "data_v4.db",
            Path.home() / ".astrbot" / "data" / "data_v4.db",
            Path(get_astrbot_data_path()) / "data_v3.db",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _range_for(self, period: str) -> tuple[datetime | None, datetime | None, str]:
        now = datetime.now(self._tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        key = str(period or "today").strip().lower()

        if key in ("today", "今天", "今日"):
            start, end, label = today, today + timedelta(days=1), "今天"
        elif key in ("yesterday", "昨天"):
            start = today - timedelta(days=1)
            end = today
            label = "昨天"
        elif key in ("week", "本周", "这周"):
            start = today - timedelta(days=today.weekday())
            end = today + timedelta(days=1)
            label = "本周"
        elif key in ("last7", "最近7天", "近7天"):
            start, end, label = today - timedelta(days=6), today + timedelta(days=1), "最近 7 天"
        elif key in ("month", "本月", "这个月"):
            start = today.replace(day=1)
            end = today + timedelta(days=1)
            label = "本月"
        elif key in ("last30", "最近30天", "近30天"):
            start, end, label = today - timedelta(days=29), today + timedelta(days=1), "最近 30 天"
        elif key in ("all", "全部", "总计"):
            start, end, label = None, None, "全部"
        elif ".." in key:
            left, right = key.split("..", 1)
            try:
                start = datetime.combine(
                    date.fromisoformat(left.strip()), time.min, tzinfo=self._tz
                )
                end = datetime.combine(
                    date.fromisoformat(right.strip()) + timedelta(days=1),
                    time.min,
                    tzinfo=self._tz,
                )
            except ValueError:
                raise ValueError("日期格式应为 YYYY-MM-DD 或 YYYY-MM-DD..YYYY-MM-DD")
            label = f"{left.strip()} 至 {right.strip()}"
        else:
            try:
                day = date.fromisoformat(key)
            except ValueError:
                raise ValueError(
                    "period 只支持 today/yesterday/week/last7/month/last30/all，或 YYYY-MM-DD"
                )
            start = datetime.combine(day, time.min, tzinfo=self._tz)
            end = start + timedelta(days=1)
            label = key
        return start, end, label

    def _fetch_rows(
        self, start: datetime | None, end: datetime | None
    ) -> list[tuple]:
        db_path = self._db_path()
        if db_path is None:
            raise RuntimeError("找不到 AstrBot 数据库")
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro",
            uri=True,
            timeout=10,
        )
        try:
            sql = (
                "SELECT created_at, provider_id, provider_model, umo, "
                "token_input_other, token_input_cached, token_output "
                "FROM provider_stats"
            )
            params: list[str] = []
            if start is not None and end is not None:
                sql += " WHERE created_at >= ? AND created_at < ?"
                params = [
                    start.astimezone(timezone.utc)
                    .replace(tzinfo=None)
                    .strftime("%Y-%m-%d %H:%M:%S.%f"),
                    end.astimezone(timezone.utc)
                    .replace(tzinfo=None)
                    .strftime("%Y-%m-%d %H:%M:%S.%f"),
                ]
            sql += " ORDER BY created_at ASC"
            return list(conn.execute(sql, params))
        finally:
            conn.close()

    # ---------- 统计 ----------

    @staticmethod
    def _parse_created_at(raw: Any) -> datetime | None:
        if isinstance(raw, datetime):
            moment = raw
        else:
            text = str(raw or "").strip()
            if not text:
                return None
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    @staticmethod
    def _fmt_tokens(value: int) -> str:
        return f"{value:,}"

    def _report(self, period: str) -> str:
        start, end, label = self._range_for(period)
        rows = self._fetch_rows(start, end)
        if not rows:
            return f"{label}还没有调用记录。"

        symbol = str(self.cfg.get("currency_symbol") or "¥")
        total_cost = 0.0
        total_other = 0
        total_cached = 0
        total_output = 0
        requests = 0
        per_model: dict[str, dict[str, float]] = defaultdict(
            lambda: {"cost": 0.0, "requests": 0, "tokens": 0}
        )
        per_kind: dict[str, dict[str, float]] = defaultdict(
            lambda: {"cost": 0.0, "requests": 0}
        )
        per_day: dict[str, float] = defaultdict(float)

        for row in rows:
            (
                created_at,
                provider_id,
                provider_model,
                umo,
                other,
                cached,
                output,
            ) = row
            moment = self._parse_created_at(created_at)
            if moment is None:
                continue
            other = int(other or 0)
            cached = int(cached or 0)
            output = int(output or 0)
            if other <= 0 and cached <= 0 and output <= 0:
                continue

            model = (provider_model or provider_id or "unknown").strip()
            rate = self._rate_for(model, moment.astimezone(self._tz))
            cost = (
                other * rate["input"]
                + cached * rate["cached_input"]
                + output * rate["output"]
            ) / 1_000_000

            total_cost += cost
            total_other += other
            total_cached += cached
            total_output += output
            requests += 1

            item = per_model[model]
            item["cost"] += cost
            item["requests"] += 1
            item["tokens"] += other + cached + output

            umo_text = str(umo or "")
            kind = "私聊" if "FriendMessage" in umo_text else "群聊" if "GroupMessage" in umo_text else "其他"
            per_kind[kind]["cost"] += cost
            per_kind[kind]["requests"] += 1

            per_day[moment.astimezone(self._tz).date().isoformat()] += cost

        lines = [
            f"{label}用量：{requests} 次调用，输入 {self._fmt_tokens(total_other)}"
            f"（缓存命中 {self._fmt_tokens(total_cached)}），输出 {self._fmt_tokens(total_output)} tokens。",
            f"估算花费：{symbol}{total_cost:.4f}",
        ]

        model_parts = [
            f"{name} {symbol}{data['cost']:.4f}（{int(data['requests'])} 次）"
            for name, data in sorted(
                per_model.items(), key=lambda item: item[1]["cost"], reverse=True
            )
        ]
        if model_parts:
            lines.append("按模型：" + "；".join(model_parts))

        kind_parts = [
            f"{name} {symbol}{data['cost']:.4f}（{int(data['requests'])} 次）"
            for name, data in sorted(
                per_kind.items(), key=lambda item: item[1]["cost"], reverse=True
            )
        ]
        if kind_parts:
            lines.append("按会话：" + "；".join(kind_parts))

        if len(per_day) > 1:
            day_parts = [
                f"{day} {symbol}{cost:.4f}"
                for day, cost in sorted(per_day.items())
            ]
            if len(day_parts) > 14:
                day_parts = day_parts[-14:]
                lines.append("最近 14 天：" + "；".join(day_parts))
            else:
                lines.append("按天：" + "；".join(day_parts))

        lines.append(
            "价格按 config.json 中每百万 token 的单价估算，"
            "高峰时段为工作日 "
            + (self.cfg.get("peak_hours") or "")
            + "（法定节假日除外，可在 holidays 里补充）。"
        )
        return "\n".join(lines)

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="query_usage_cost")
    async def query_usage_cost(
        self, event: AstrMessageEvent, period: str = "today"
    ) -> str:
        """查询机器人自己的 token 用量和估算花费。仅管理员可用。

        Args:
            period(string): 统计区间。可选 today、yesterday、week、last7、month、last30、all，或具体日期 YYYY-MM-DD，或 YYYY-MM-DD..YYYY-MM-DD。
        """
        if not self._is_admin(event):
            return "error: 只有管理员可以查询用量花费。"
        if not self.cfg.get("enabled", True):
            return "error: 用量统计插件已在 config.json 中关闭。"
        try:
            return self._report(period)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[usage_cost] 查询失败：{exc}")
            return f"error: 查询用量失败：{exc}"
