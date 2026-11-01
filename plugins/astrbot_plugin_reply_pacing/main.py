from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart

try:
    from astrbot.api.message_components import At, Reply
except Exception:  # noqa: BLE001
    At = None  # type: ignore[assignment]
    Reply = None  # type: ignore[assignment]

# --- portable data paths (resolved from AstrBot's data dir; no absolute paths) ---
def _plugin_data_dir(name: str) -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        base = Path(get_astrbot_data_path())
    except Exception:  # 旧版 AstrBot 没有这个工具时的兜底
        base = Path.home() / ".astrbot" / "data"
    return base / "plugin_data" / name


_PERSONA_GROWTH_DIR = _plugin_data_dir("astrbot_plugin_persona_growth")
_SELF_MEMORY_DIR = _plugin_data_dir("astrbot_plugin_self_memory")
_INTEREST_PROFILE_DIR = _plugin_data_dir("astrbot_plugin_interest_profile")
_TOPIC_PROFICIENCY_DIR = _plugin_data_dir("astrbot_plugin_topic_proficiency")
_GROUP_PROACTIVE_DIR = _plugin_data_dir("astrbot_plugin_group_proactive")

_STATE_DIR = _plugin_data_dir("astrbot_plugin_reply_pacing")
try:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    _STATE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = Path(__file__).with_name("config.json")
STATE_DIR = _STATE_DIR
STATE_FILE = STATE_DIR / "state.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    # 回复前等待：数最近 window_seconds 秒内「别人」在这个会话发了多少条，
    # 条数越多等得越久；窗口滑过去自动回落，不需要额外的“减少”逻辑。
    "window_seconds": 60.0,
    "window_floor_messages": 2,
    "window_mid_messages": 8,
    "window_max_messages": 20,
    # 分段线性：条数 <= floor 取下限；floor..mid 升到中段；mid..max 升到封顶。
    "private_min_delay_seconds": 3.0,
    "private_mid_delay_seconds": 7.0,
    "private_max_delay_seconds": 10.0,
    "group_min_delay_seconds": 4.0,
    "group_mid_delay_seconds": 8.0,
    "group_max_delay_seconds": 10.0,
    # 连发合并：等到安静这么久，或者最多多等这么久，只回一次。
    "quiet_window_seconds": 2.5,
    "max_debounce_wait_seconds": 8.0,
    "jitter_seconds": 1.0,
    # 秒回阶梯：她刚发出去，对方在 streak_gap_seconds 内就回，连续几次说明
    # 是在对刷（多数情况是机器人）。私聊里任何消息都算证据；群里只有
    # @她 / 引用她 的消息才算，别人之间的快聊不算。慢一条回落 streak_decay 次。
    "streak_gap_seconds": 2.0,
    "streak_ladder_1": 4,
    "streak_ladder_2": 8,
    "streak_ladder_3": 12,
    "streak_delay_1_seconds": 30.0,
    "streak_delay_2_seconds": 90.0,
    "streak_delay_3_seconds": 300.0,
    "streak_decay": 1,
    # 同一个人间隔小于它算「同一个回话」，一口气打几行字只记一次。
    "streak_burst_seconds": 1.0,
    "streak_ttl_seconds": 600.0,
    # 管理员私聊不等待。
    "admin_exempt": True,
    # 同一条回复被拆成多条发送时，之间留一点间隔。
    "send_gap_seconds": 1.5,
    "send_max_wait_seconds": 5.0,
    # 状态保留天数，避免 state.json 无限增长。
    "state_keep_days": 7,
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


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


class Main(star.Star):
    """回复前等待 + 连发合并 + 按消息密度动态拉长。

    它不判断对方是不是机器人，也不限制“一分钟最多几条”。
    只数最近 60 秒别人在该会话发了多少条：发得勤就多等一会儿，
    冷下来窗口自然滑空，回到基础延迟；对方一次发好几条，只合并成一次回复。
    主动任务和被动回复走同一套延迟、同一份窗口。
    """

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = _load_config()
        self.state = self._load_state()
        # umo -> 上一次真正发出回复的时间（on_decorating_result 用）
        self._last_sent: dict[str, float] = {}
        self._admin_ids: list[str] | None = None
        self._save_at = 0.0

    # ---------- 基础工具 ----------

    def _now(self) -> float:
        return time.time()

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def _cfg_float(self, key: str, default: float, minimum: float = 0.0) -> float:
        return _as_float(self.cfg.get(key, default), default, minimum)

    def _cfg_int(self, key: str, default: int, minimum: int = 0) -> int:
        return _as_int(self.cfg.get(key, default), default, minimum)

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

    # ---------- 状态 ----------

    def _load_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"version": 2, "sessions": {}}
        try:
            if STATE_FILE.exists():
                loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(
                    loaded.get("sessions"), dict
                ):
                    state = loaded
                    state["version"] = 2
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"回复节奏：状态读取失败，从空状态开始：{exc}")
        return state

    def _session(self, umo: str) -> dict[str, Any]:
        sessions = self.state.setdefault("sessions", {})
        item = sessions.get(umo)
        if not isinstance(item, dict):
            item = {}
            sessions[umo] = item
        item.setdefault("seq", 0)
        item.setdefault("last_inbound", 0.0)
        item.setdefault("last_reply", 0.0)
        item.setdefault("inbound", [])
        item.setdefault("pending", [])
        item.setdefault("dropped", [])
        item.setdefault("streaks", {})
        item.setdefault("last_out", 0.0)
        return item

    def _trim_sessions(self) -> None:
        sessions = self.state.get("sessions")
        if not isinstance(sessions, dict):
            return
        keep_days = self._cfg_float("state_keep_days", 7.0, 1.0)
        cutoff = self._now() - keep_days * 86400
        for umo in list(sessions):
            item = sessions.get(umo)
            if not isinstance(item, dict):
                sessions.pop(umo, None)
                continue
            last = _as_float(item.get("last_inbound"), 0.0)
            if last < cutoff:
                sessions.pop(umo, None)

    def _schedule_save(self, *, force: bool = False) -> None:
        now = self._now()
        if not force and now - self._save_at < 2.0:
            return
        self._save_at = now
        self._trim_sessions()
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = json.dumps(self.state, ensure_ascii=False, indent=2)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"回复节奏：状态保存失败：{exc}")

    # ---------- 节奏计算：按消息密度算延迟 ----------

    def _inbound_window(self, item: dict[str, Any], now: float) -> list[float]:
        """最近 window_seconds 秒内别人发的消息时间戳，顺带滑掉过期项。"""
        window = self._cfg_float("window_seconds", 60.0, 10.0)
        raw = item.get("inbound")
        raw = raw if isinstance(raw, list) else []
        cutoff = now - window
        stamps = [t for t in (_as_float(x, 0.0) for x in raw) if t >= cutoff]
        item["inbound"] = stamps
        return stamps

    def _delay_for_count(self, count: int, *, private: bool) -> float:
        """分段线性：条数越多等得越久，上下限和拐点都由配置决定。"""
        floor_n = self._cfg_int("window_floor_messages", 2, 0)
        mid_n = self._cfg_int("window_mid_messages", 8, 1)
        max_n = self._cfg_int("window_max_messages", 20, 1)
        mid_n = max(mid_n, floor_n + 1)
        max_n = max(max_n, mid_n + 1)

        if private:
            low = self._cfg_float("private_min_delay_seconds", 3.0, 0.0)
            mid = self._cfg_float("private_mid_delay_seconds", 7.0, 0.0)
            cap = self._cfg_float("private_max_delay_seconds", 10.0, 0.0)
        else:
            low = self._cfg_float("group_min_delay_seconds", 4.0, 0.0)
            mid = self._cfg_float("group_mid_delay_seconds", 8.0, 0.0)
            cap = self._cfg_float("group_max_delay_seconds", 10.0, 0.0)
        mid = max(mid, low)
        cap = max(cap, mid)

        if count <= floor_n:
            delay = low
        elif count <= mid_n:
            delay = low + (mid - low) * (count - floor_n) / (mid_n - floor_n)
        elif count <= max_n:
            delay = mid + (cap - mid) * (count - mid_n) / (max_n - mid_n)
        else:
            delay = cap

        jitter = self._cfg_float("jitter_seconds", 1.0, 0.0)
        if jitter > 0:
            delay += random.uniform(0.0, jitter)
        return max(0.0, delay)

    # ---------- 秒回阶梯：连续「她一说完对方就回」 ----------

    @staticmethod
    def _message_targets_me(event: AstrMessageEvent) -> bool:
        """这条群消息是不是冲她说的（@她 / 引用她的消息）。

        群里消息多、真人也回得快，只看速度会把「别人之间的快聊」误当成
        在跟她对刷。@全体 不算：那是通知，不是一对一地追问她。
        """
        if At is None or Reply is None:
            return False
        try:
            self_id = str(event.get_self_id())
            message = getattr(event.message_obj, "message", None) or []
        except Exception:  # noqa: BLE001
            return False
        for seg in message:
            if isinstance(seg, At) and str(getattr(seg, "qq", "")) == self_id:
                return True
            if (
                isinstance(seg, Reply)
                and str(getattr(seg, "sender_id", "")) == self_id
            ):
                return True
        return False

    def _streak_ladder(self, count: int) -> float:
        if count >= self._cfg_int("streak_ladder_3", 12, 1):
            return self._cfg_float("streak_delay_3_seconds", 300.0, 0.0)
        if count >= self._cfg_int("streak_ladder_2", 8, 1):
            return self._cfg_float("streak_delay_2_seconds", 90.0, 0.0)
        if count >= self._cfg_int("streak_ladder_1", 4, 1):
            return self._cfg_float("streak_delay_1_seconds", 30.0, 0.0)
        return 0.0

    def _update_streak(
        self,
        item: dict[str, Any],
        sender: str,
        now: float,
        anchor: float,
        *,
        evidence: bool,
    ) -> int:
        """更新这个发送者的「连续秒回」次数，返回当前值。

        evidence=False（群里这条不是在跟她说）时原样返回：别人聊得再快，
        也不该动到某个发送者的计数，更不该让她对别人的回复变慢。
        """
        streaks = item.get("streaks")
        if not isinstance(streaks, dict):
            streaks = {}
            item["streaks"] = streaks
        ttl = self._cfg_float("streak_ttl_seconds", 600.0, 0.0)
        if ttl > 0:
            for key in list(streaks):
                entry = streaks.get(key)
                if not isinstance(entry, dict) or now - _as_float(
                    entry.get("ts"), 0.0
                ) > ttl:
                    streaks.pop(key, None)

        key = sender or "_unknown"
        entry = streaks.get(key)
        count = _as_int(entry.get("n"), 0) if isinstance(entry, dict) else 0
        if not evidence:
            return count

        gap = self._cfg_float("streak_gap_seconds", 2.0, 0.0)
        burst = self._cfg_float("streak_burst_seconds", 1.0, 0.0)
        last_msg = _as_float(entry.get("last_msg"), 0.0) if isinstance(entry, dict) else 0.0
        if burst > 0 and last_msg > 0 and (now - last_msg) < burst:
            # 同一个人连发几句算一次回话，否则「一口气打四行字」会被当成四连秒回。
            streaks[key] = {"n": count, "ts": now, "last_msg": now}
            return count

        fast = anchor > 0 and gap > 0 and (now - anchor) <= gap
        if fast:
            count += 1
        else:
            count = max(0, count - self._cfg_int("streak_decay", 1, 0))
        streaks[key] = {"n": count, "ts": now, "last_msg": now}
        return count

    def _ladder_for_event(
        self, item: dict[str, Any], event: AstrMessageEvent, now: float, *,
        private: bool,
    ) -> float:
        try:
            sender = str(event.get_sender_id() or "")
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:  # noqa: BLE001
            return 0.0
        anchor = max(
            _as_float(item.get("last_out"), 0.0),
            _as_float(self._last_sent.get(umo), 0.0),
        )
        evidence = private or self._message_targets_me(event)
        return self._streak_ladder(
            self._update_streak(item, sender, now, anchor, evidence=evidence)
        )

    # ---------- 连发合并 ----------

    @staticmethod
    def _event_text(event: AstrMessageEvent) -> str:
        try:
            text = str(getattr(event, "message_str", "") or "")
        except Exception:  # noqa: BLE001
            return ""
        return text.strip()[:500]

    def _register_inbound(
        self, item: dict[str, Any], event: AstrMessageEvent, now: float
    ) -> int:
        item["seq"] = _as_int(item.get("seq"), 0) + 1
        item["last_inbound"] = now
        stamps = self._inbound_window(item, now)
        stamps.append(now)
        item["inbound"] = stamps
        try:
            name = str(event.get_sender_name() or "")
        except Exception:  # noqa: BLE001
            name = ""
        text = self._event_text(event)
        if text:
            pending = item.setdefault("pending", [])
            pending.append(
                {
                    "seq": item["seq"],
                    "ts": now,
                    "name": name,
                    "text": text,
                }
            )
            # 只留最近 20 条 / 10 分钟，防止状态无限增长。
            cutoff = now - 600
            item["pending"] = [
                p
                for p in pending
                if _as_float(p.get("ts"), 0.0) >= cutoff
            ][-20:]
        return item["seq"]

    def _take_merged(self, item: dict[str, Any], my_seq: int) -> str:
        """把等待期间被让位的消息拼成一段临时上下文。"""
        dropped = {
            _as_int(seq, 0) for seq in (item.get("dropped") or [])
        }
        pending = item.get("pending") or []
        merged = [
            p
            for p in pending
            if _as_int(p.get("seq"), 0) in dropped
            and _as_int(p.get("seq"), 0) != my_seq
        ]
        # 清掉这次处理过的；没被合并的（最新那条自己）也清掉。
        item["pending"] = [
            p
            for p in pending
            if _as_int(p.get("seq"), 0) != my_seq
            and _as_int(p.get("seq"), 0) not in dropped
        ]
        item["dropped"] = []
        if not merged:
            return ""
        lines = ["【等待期间对方又发的消息，一并看，不要逐条重复回复】"]
        for p in merged[-10:]:
            name = str(p.get("name") or "").strip()
            text = str(p.get("text") or "").strip()
            if text:
                lines.append(f"{name}: {text}" if name else text)
        return "\n".join(lines)

    # ---------- 主钩子：回复前等待 ----------

    async def _pace_wait(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        """按消息密度等一会儿，返回本次等待的上下文；不需要等则返回 None。

        被动回复走 on_waiting_llm_request，主动任务（cron）不经过那个钩子，
        走 on_agent_begin。两边都调这里：延迟逻辑和窗口各只有一份。
        """
        if not self.cfg.get("enabled", True):
            return None
        try:
            if event.is_stopped():
                return None
        except Exception:  # noqa: BLE001
            pass
        if event.get_extra("_rp_paced"):
            return None

        try:
            private = bool(event.is_private_chat())
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:  # noqa: BLE001
            return None
        if not umo:
            return None

        if private and self.cfg.get("admin_exempt", True) and self._is_admin(event):
            return None

        event.set_extra("_rp_paced", True)
        item = self._session(umo)
        now = self._now()
        # 主动任务和被动回复共用这一份窗口：只有“别人发的消息”才计入。
        try:
            is_cron = event.get_extra("cron_job") is not None
        except Exception:  # noqa: BLE001
            is_cron = False
        if is_cron:
            count = len(self._inbound_window(item, now))
            my_seq = 0
            ladder = 0.0
        else:
            my_seq = self._register_inbound(item, event, now)
            count = len(item.get("inbound") or [])
            ladder = self._ladder_for_event(
                item, event, now, private=private
            )

        density = self._delay_for_count(count, private=private)
        delay = max(density, ladder)

        if ladder > 0:
            logger.info(
                "回复节奏：%s 连续秒回，本次按阶梯等 %.1f 秒（密度延迟 %.1f 秒）",
                umo,
                ladder,
                density,
            )
        elif is_cron or count >= self._cfg_int(
            "window_floor_messages", 2, 0
        ) + 1:
            logger.info(
                "回复节奏：%s%s 最近 %d 秒别人发了 %d 条，本次等 %.1f 秒",
                "主动任务 " if is_cron else "",
                umo,
                int(self._cfg_float("window_seconds", 60.0, 10.0)),
                count,
                delay,
            )

        # 等基础延迟；期间如果对方还在发，再等到安静 quiet_window。
        quiet = self._cfg_float("quiet_window_seconds", 2.5, 0.0)
        extra = self._cfg_float("max_debounce_wait_seconds", 8.0, 0.0)
        target = now + delay
        hard_deadline = target + extra
        while True:
            current = self._now()
            if current >= hard_deadline:
                break
            last_inbound = _as_float(item.get("last_inbound"), current)
            if current >= target and current - last_inbound >= quiet:
                break
            await self._sleep(min(0.2, max(0.01, hard_deadline - current)))

        return {"item": item, "is_cron": is_cron, "seq": my_seq}

    @filter.on_waiting_llm_request()
    async def pace_before_reply(self, event: AstrMessageEvent) -> None:
        ctx = await self._pace_wait(event)
        if ctx is None:
            return
        item = ctx["item"]
        is_cron = ctx["is_cron"]
        my_seq = ctx["seq"]

        # 等待期间来了更新的消息：这条让位，由最新那条统一回复。
        # 主动任务没有对应的入站消息，不参与让位，否则会被正在聊的人顶掉。
        if not is_cron and _as_int(item.get("seq"), 0) != my_seq:
            item.setdefault("dropped", []).append(my_seq)
            event.stop_event()
            self._schedule_save()
            return

        if not is_cron:
            merged = self._take_merged(item, my_seq)
            if merged:
                event.set_extra("_rp_merged_text", merged)

        item["last_reply"] = self._now()
        self._schedule_save()

    @filter.on_agent_begin()
    async def pace_before_agent(self, event: AstrMessageEvent, run_context) -> None:
        """主动任务走 cron → _woke_main_agent，不经过 on_waiting_llm_request。

        被动回复到这里时 _rp_paced 已经设过，直接跳过，不会等两遍。
        """
        ctx = await self._pace_wait(event)
        if ctx is None:
            return
        ctx["item"]["last_reply"] = self._now()
        self._schedule_save()

    @filter.on_llm_request()
    async def inject_merged_text(self, event: AstrMessageEvent, req) -> None:
        """把被合并掉的连发消息作为临时上下文交给模型，不写回历史。"""
        merged = event.get_extra("_rp_merged_text")
        if not merged:
            return
        try:
            if getattr(req, "extra_user_content_parts", None) is None:
                req.extra_user_content_parts = []
            req.extra_user_content_parts.append(
                TextPart(text=str(merged)).mark_as_temp()
            )
            # 工具循环里会多次走 on_llm_request，注入一次就清掉。
            event.set_extra("_rp_merged_text", "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"回复节奏：合并消息注入失败：{exc}")

    # ---------- 发送侧：只处理分段消息之间的间隔 ----------

    @filter.on_decorating_result()
    async def space_split_messages(self, event: AstrMessageEvent) -> None:
        if not self.cfg.get("enabled", True):
            return
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        if result is None or not getattr(result, "chain", None):
            return
        try:
            if not result.is_model_result():
                return
        except Exception:  # noqa: BLE001
            pass

        try:
            private = bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            private = False
        if private and self.cfg.get("admin_exempt", True) and self._is_admin(event):
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        gap = self._cfg_float("send_gap_seconds", 1.5, 0.0)
        max_wait = self._cfg_float("send_max_wait_seconds", 5.0, 0.0)

        now = self._now()
        last = self._last_sent.get(umo)
        wait = 0.0
        if gap > 0 and last is not None:
            wait = gap - (now - last)
        if wait > max_wait:
            wait = max_wait
        if wait > 0:
            self._last_sent[umo] = now + wait
            await self._sleep(wait)
        # 真正发出去的时间：「连续秒回」以它为起点，不能用开始生成的时间，
        # 否则生成耗时会被算进对方的间隔里，1 秒回的机器人永远够不到阈值。
        sent_at = self._now()
        self._last_sent[umo] = sent_at
        self._session(umo)["last_out"] = sent_at
        self._schedule_save()
