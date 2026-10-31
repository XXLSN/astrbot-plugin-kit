"""兴趣偏好总结：从娱乐冲浪日记里抽出「她接触了什么」，再让糯糯本人打分。

两道模型调用，职责分开：
- 调用 A：整理员（无人设），只从日记原文里转录 topic + 原文出处，不产出态度。
- 调用 B：糯糯本人（完整人设），给 A 抽出来的每一项打分。

分数是本地按固定公式算的，模型只给「这次多喜欢/多讨厌」的强度，
所以同一批事件任何时候重算结果都一样。

只处理 mode == "fun" 的日记：学说话（style）和聊天（chat）都不进兴趣。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import filter

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

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PLUGIN_DIR / "config.json"
DATA_DIR = _INTEREST_PROFILE_DIR
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    pass
PREFS_FILE = DATA_DIR / "prefs.json"
SCORE_LOG = DATA_DIR / "score_log.jsonl"
EVENTS_FILE = DATA_DIR / "events.jsonl"
STATE_FILE = DATA_DIR / "state.json"

TIER_ORDER = ["非常喜欢", "喜欢", "轻微喜欢", "中立", "轻微讨厌", "讨厌", "非常讨厌"]
TIER_BOUNDARIES = (5.0, 15.0, 36.0, 65.0, 86.0, 96.0)
BASE_WEIGHT = {"喜欢": 12.0, "讨厌": -12.0, "害怕": -18.0, "无感": 0.0}
DECAY_SCALE = 72.0

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "provider_id": "openai/deepseek-flash",
    "timezone": "Asia/Shanghai",
    "diary_path": "",
    "proficiency_path": "",
    "poll_interval_seconds": 60,
    "startup_delay_seconds": 90,
    "max_entries_per_tick": 3,
    "half_life_days": 45,
    "profile_top_n": 24,
    "profile_max_chars": 200,
    "hysteresis": 3.0,
    "dry_run": False,
}

SUMMARY_SYSTEM = """你是整理员，不是糯糯。下面是一篇日记原文。
只做转录：找出这篇日记里她**实际接触到的具体东西**（作品、游戏、话题、活动、食物、动物等）。
每一项给出：
- topic: 短名词，2-6 个字，尽量用日记里的说法
- cue: 日记里出现的那个词
- quote: 日记里逐字存在的一句，包含这个 topic 或它的同义说法

规则：
- 只认日记里写到的，不补充、不推断、不评价。
- 不根据「看了很多」推断喜欢，态度不归你管。
- 如果这篇只写了说话方式、语气、口头禅，没有具体东西，输出 []。
- 只输出 JSON 数组，不要解释、不要 Markdown。格式：
[{"topic": "动画", "cue": "新番", "quote": "这季新番好看"}]
"""

SCORE_RUBRIC = """现在不是聊天，是在给自己记账。
下面是这次看到的东西，以及你自己写的日记。给每一样打个分，记在心里，不用嘴硬。
kind 四选一：喜欢 / 讨厌 / 害怕 / 无感
  - 喜欢：会主动接触
  - 讨厌：不想接触、觉得烦
  - 害怕：不敢接触、提起就不舒服
  - 无感：无所谓
intensity 0-100：
  100 = 提到就眼睛亮，会主动去追
  70 = 确实喜欢，会看/会玩
  50 = 不冷不热
  30 = 有点烦
  0 = 完全不想碰
line：用你自己的口吻说一句（可以傲娇、可以嘴硬），只是留个记录，不影响分数。
只给下面列表里的东西打分，不要新增。没有感觉就给 无感 / 50。
只输出 JSON 数组，不要解释、不要 Markdown。格式：
[{"topic": "动画", "kind": "喜欢", "intensity": 70, "line": "还行吧"}]
"""


def _load_json(path: Path, expect: type) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return expect()
    return data if isinstance(data, expect) else expect()


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data: Any = None
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r"\[[\s\S]*\]", raw)
        if match:
            try:
                data = json.loads(match.group(0))
            except ValueError:
                data = None
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _tier_of(pref: float) -> str:
    if pref < 5:
        return "非常讨厌"
    if pref < 15:
        return "讨厌"
    if pref < 36:
        return "轻微讨厌"
    if pref < 65:
        return "中立"
    if pref < 86:
        return "轻微喜欢"
    if pref < 96:
        return "喜欢"
    return "非常喜欢"


def _tier_with_hysteresis(pref: float, prev: str | None, margin: float) -> str:
    raw = _tier_of(pref)
    if not prev or prev == raw or prev not in TIER_ORDER:
        return raw
    if abs(TIER_ORDER.index(prev) - TIER_ORDER.index(raw)) > 1:
        return raw
    for boundary in TIER_BOUNDARIES:
        if abs(pref - boundary) < margin:
            return prev
    return raw


class Main(star.Star):
    """把娱乐冲浪日记里的兴趣抽出来，让糯糯自己打分，写成 prefs.json。"""

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = self._load_config()
        self._lock = asyncio.Lock()
        self._persona_cache: tuple[float, str] = (0.0, "")

    # ---------- 配置 ----------

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        data = _load_json(CONFIG_FILE, dict)
        if data:
            cfg.update(data)
        return cfg

    # ---------- 启动 ----------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self.cfg.get("enabled", True):
            logger.info("兴趣偏好总结已关闭（config.json enabled=false）")
            return
        old = getattr(self, "_loop_task", None)
        if isinstance(old, asyncio.Task) and not old.done():
            old.cancel()
        self._loop_task = asyncio.create_task(self._run_loop(), name="interest_profile_loop")
        logger.info(
            "兴趣偏好总结已启动：每 %s 秒看一次娱乐日记",
            self.cfg.get("poll_interval_seconds", 60),
        )

    async def _run_loop(self) -> None:
        delay = int(self.cfg.get("startup_delay_seconds", 90) or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        interval = max(20, int(self.cfg.get("poll_interval_seconds", 60) or 60))
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[interest_profile] 轮询出错")
            await asyncio.sleep(interval)

    # ---------- 主流程 ----------

    async def _tick(self) -> None:
        state = _load_json(STATE_FILE, dict)
        processed = [str(item) for item in (state.get("processed") or [])]
        done = set(processed)
        entries = [
            entry
            for entry in _load_json(Path(str(self.cfg.get("diary_path"))), list)
            if isinstance(entry, dict)
        ]
        pending = [
            entry
            for entry in entries
            if str(entry.get("mode") or "") == "fun"
            and str(entry.get("at") or "") not in done
            and str(entry.get("text") or "").strip()
        ]
        if not pending:
            return
        limit = max(1, int(self.cfg.get("max_entries_per_tick", 3) or 3))
        pending = pending[:limit]
        changed = False
        for entry in pending:
            at = str(entry.get("at") or "")
            try:
                finished = await self._process_entry(entry)
            except Exception:  # noqa: BLE001
                logger.exception("[interest_profile] 处理日记失败：%s", at)
                finished = False
            # 模型没拿到/调用失败的不记账，下一轮再试；只有处理完的才移出队列。
            if not finished:
                continue
            processed.append(at)
            done.add(at)
            changed = True
        if changed:
            state["processed"] = processed[-500:]
            state["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
            STATE_FILE.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._recompute()

    async def _process_entry(self, entry: dict[str, Any]) -> bool:
        """返回 True 表示这篇处理完了（含「没有可记的兴趣」）；False 表示下次再试。"""
        text = str(entry.get("text") or "").strip()
        if not text:
            return True
        if self.cfg.get("dry_run", False):
            logger.info("[interest_profile][dry_run] 会处理：%s", text[:60])
            return False
        provider = await self._provider()
        if provider is None:
            logger.warning("[interest_profile] 没有可用模型，这篇留到下一轮")
            return False

        topics = await self._summarize(provider, text)
        if topics is None:
            return False
        if not topics:
            logger.info("[interest_profile] 这篇没有可记的兴趣：%s", text[:40])
            return True
        scored = await self._score(provider, text, topics)
        if scored is None:
            return False
        if not scored:
            logger.info("[interest_profile] 糯糯这次没给分：%s", text[:40])
            return True

        at = str(entry.get("at") or "")
        today = at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", at) else datetime.now().date().isoformat()
        for item in scored:
            _append_jsonl(
                EVENTS_FILE,
                {
                    "at": at,
                    "date": today,
                    "topic": item["topic"],
                    "kind": item["kind"],
                    "intensity": item["intensity"],
                    "line": item.get("line", ""),
                    "src": f"diary:{at}",
                    "quote": item.get("quote", ""),
                },
            )
        logger.info(
            "[interest_profile] 记下 %s 条兴趣事件：%s",
            len(scored),
            "、".join(f"{item['topic']}({item['kind']}{item['intensity']})" for item in scored),
        )
        return True

    # ---------- 调用 A：整理员 ----------

    async def _summarize(self, provider, text: str) -> list[dict[str, str]] | None:
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt="日记原文：\n" + text,
                    system_prompt=SUMMARY_SYSTEM,
                    contexts=[],
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            logger.warning("[interest_profile] 整理员超时")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[interest_profile] 整理员调用失败：%s", exc)
            return None
        raw = str(getattr(response, "completion_text", "") or "")
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in _parse_json_array(raw):
            topic = str(item.get("topic") or "").strip()[:12]
            quote = str(item.get("quote") or "").strip()
            if not topic or topic in seen:
                continue
            # 没有原文出处的事件一律不要，防止整理员替她编。
            if not quote or quote not in text:
                logger.info("[interest_profile] 丢弃无出处的 topic：%s", topic)
                continue
            seen.add(topic)
            out.append(
                {
                    "topic": topic,
                    "cue": str(item.get("cue") or "").strip()[:12],
                    "quote": quote[:80],
                }
            )
        return out[:6]

    # ---------- 调用 B：糯糯打分 ----------

    async def _score(
        self, provider, text: str, topics: list[dict[str, str]]
    ) -> list[dict[str, Any]] | None:
        persona = await self._persona_prompt()
        system = (persona + "\n\n---\n\n" + SCORE_RUBRIC).strip() if persona else SCORE_RUBRIC
        listing = "\n".join(
            f"- {item['topic']}（原文：{item['quote']}）" for item in topics
        )
        prompt = f"这次看到的东西：\n{listing}\n\n你写的日记：\n{text}"
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system, contexts=[]),
                timeout=40,
            )
        except asyncio.TimeoutError:
            logger.warning("[interest_profile] 打分超时")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[interest_profile] 打分调用失败：%s", exc)
            return None
        raw = str(getattr(response, "completion_text", "") or "")
        allowed_topics = {item["topic"]: item for item in topics}
        out: list[dict[str, Any]] = []
        for item in _parse_json_array(raw):
            topic = str(item.get("topic") or "").strip()
            if topic not in allowed_topics:
                continue
            kind = str(item.get("kind") or "").strip()
            if kind not in BASE_WEIGHT:
                continue
            try:
                intensity = float(item.get("intensity", 50))
            except (TypeError, ValueError):
                intensity = 50.0
            intensity = max(0.0, min(100.0, intensity))
            out.append(
                {
                    "topic": topic,
                    "kind": kind,
                    "intensity": round(intensity, 1),
                    "line": str(item.get("line") or "").strip()[:60],
                    "quote": allowed_topics[topic]["quote"],
                }
            )
        # 一条都没解析出来说明这次输出坏了，留到下一轮重试，不要当成「她无感」记账。
        return out[:6] or None

    async def _persona_prompt(self) -> str:
        now_ts = datetime.now(timezone.utc).timestamp()
        cached_at, cached = self._persona_cache
        if cached and now_ts - cached_at < 600:
            return cached
        prompt = ""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(None)
            if isinstance(persona, dict):
                prompt = str(persona.get("prompt") or "")
            else:
                prompt = str(getattr(persona, "prompt", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[interest_profile] 取人设失败：%s", exc)
        self._persona_cache = (now_ts, prompt)
        return prompt

    async def _provider(self):
        pid = str(self.cfg.get("provider_id") or "").strip()
        if pid:
            try:
                provider = self.context.get_provider_by_id(pid)
            except Exception:  # noqa: BLE001
                provider = None
            if provider is not None:
                return provider
        try:
            return await self.context.get_using_provider_async("")
        except Exception:  # noqa: BLE001
            return None

    # ---------- 打分与落盘 ----------

    def _load_events(self) -> list[dict[str, Any]]:
        try:
            lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-5000:]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("topic"):
                out.append(item)
        return out

    def _event_weight(self, event: dict[str, Any], now: datetime) -> float:
        base = BASE_WEIGHT.get(str(event.get("kind") or ""), 0.0)
        try:
            intensity = float(event.get("intensity", 50))
        except (TypeError, ValueError):
            intensity = 50.0
        intensity = max(0.0, min(100.0, intensity)) / 100.0
        try:
            at = datetime.fromisoformat(str(event.get("at") or event.get("date") or ""))
        except ValueError:
            days = 0.0
        else:
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            days = max(0.0, (now - at).total_seconds() / 86400)
        half_life = float(self.cfg.get("half_life_days", 45) or 45)
        return base * intensity * (0.5 ** (days / max(1.0, half_life)))

    def _recompute(self) -> None:
        events = self._load_events()
        if not events:
            return
        now = datetime.now(timezone.utc)
        prev = {
            str(item.get("topic")): str(item.get("tier"))
            for item in (_load_json(PREFS_FILE, dict).get("items") or [])
            if isinstance(item, dict)
        }
        prof_map = self._load_proficiency()
        margin = float(self.cfg.get("hysteresis", 3) or 0)
        by_topic: dict[str, list[tuple[dict[str, Any], float]]] = {}
        for event in events:
            weight = self._event_weight(event, now)
            by_topic.setdefault(str(event["topic"]), []).append((event, weight))

        items: list[dict[str, Any]] = []
        for topic, rows in by_topic.items():
            total = sum(weight for _event, weight in rows)
            pref = 50.0 + 50.0 * math.tanh(total / DECAY_SCALE)
            tier = _tier_with_hysteresis(pref, prev.get(topic), margin)
            fear_w = sum(abs(w) for e, w in rows if e.get("kind") == "害怕")
            hate_w = sum(abs(w) for e, w in rows if e.get("kind") == "讨厌")
            like_w = sum(abs(w) for e, w in rows if e.get("kind") == "喜欢")
            bucket = "害怕" if fear_w > hate_w and fear_w > 0 else tier
            prof = round(float(prof_map.get(topic, 0) or 0), 1)
            items.append(
                {
                    "topic": topic,
                    "pref": round(pref, 1),
                    "tier": bucket,
                    "score_tier": tier,
                    "prof": prof,
                    "events": len(rows),
                }
            )
            _append_jsonl(
                SCORE_LOG,
                {
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "topic": topic,
                    "sum": round(total, 2),
                    "pref": round(pref, 1),
                    "tier": bucket,
                    "score_tier": tier,
                    "prev_tier": prev.get(topic) or "",
                    "events": len(rows),
                    "like_w": round(like_w, 2),
                    "hate_w": round(hate_w, 2),
                    "fear_w": round(fear_w, 2),
                    "prof": prof,
                },
            )

        items.sort(key=lambda item: abs(float(item["pref"]) - 50.0), reverse=True)
        top_n = max(4, int(self.cfg.get("profile_top_n", 24) or 24))
        items = items[:top_n]
        line = self._render_line(items)
        payload = {
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "line": line,
            "items": items,
        }
        PREFS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _render_line(self, items: list[dict[str, Any]]) -> str:
        buckets: dict[str, list[str]] = {}
        for item in items:
            tier = str(item.get("tier") or "")
            if tier not in TIER_ORDER and tier != "害怕":
                continue
            prof = float(item.get("prof") or 0)
            name = str(item["topic"])
            if prof > 0:
                name += str(int(round(prof)))
            buckets.setdefault(tier, []).append(name)
        order = ["非常喜欢", "喜欢", "轻微喜欢", "中立", "轻微讨厌", "讨厌", "非常讨厌", "害怕"]
        parts = [f"{tier}:{'、'.join(buckets[tier])}" for tier in order if buckets.get(tier)]
        line = "；".join(parts)
        limit = max(80, int(self.cfg.get("profile_max_chars", 200) or 200))
        while len(line) > limit and len(parts) > 1:
            parts.pop()
            line = "；".join(parts)
        return line

    def _load_proficiency(self) -> dict[str, float]:
        path = Path(str(self.cfg.get("proficiency_path") or ""))
        if not str(path).strip():
            return {}
        data = _load_json(path, dict)
        topics = data.get("topics") if isinstance(data, dict) else None
        if not isinstance(topics, dict):
            return {}
        out: dict[str, float] = {}
        for topic, value in topics.items():
            if isinstance(value, dict):
                value = value.get("prof")
            try:
                out[str(topic)] = float(value)
            except (TypeError, ValueError):
                continue
        return out
