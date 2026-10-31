from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

CONFIG_FILE = Path(__file__).with_name("config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    # 历史里最多保留几张原图；当前这条消息带的图不计入
    "keep_recent_images": 2,
    # 清空历史 assistant 的思考链内容（DeepSeek 不看旧推理，字段保留为空）
    "clear_historical_think": True,
    # 最近 N 条工具结果保留原文，更早的截断
    "keep_recent_tool_results": 5,
    "tool_result_max_chars": 100,
    # 历史里的 system_reminder 只压缩时间，User ID / Nickname / Group name 不动
    "compact_historical_reminders": True,
    # 历史里的人格状态块（日记 / 脾气 / 此刻 / 最近说过的话）只留占位符。
    # 当前这轮的那份不在 req.contexts 里，所以历史里剩下的全是过期的。
    "trim_persona_blocks": True,
    # 历史里最多保留几段群聊快照（当前请求新注入的那段不计入）
    # 单段实测 1.8k–3.2k 字符。配 block_trim_quantum=3 时实际保留 N ~ N+2 段，
    # 所以写 2 实际是留 2–4 段（约最近三次触发点），这是「群记忆 vs 花费」的折中。
    "keep_recent_group_context_blocks": 2,
    # 截断点每多少条才往前挪一格（挪得越碎，前缀缓存越容易失效）
    "trim_quantum": 4,
    # 群聊快照一段有一两千字，步长要小一点，否则会多留太多过期快照
    "block_trim_quantum": 3,
    # 上线补读（backlog）只需要接着离线那几句答，离线内容本来就在提示词里。
    # 40 条约 20 轮，够包住「离线前那一段对话」；再往上就是纯重复了。
    "cron_backlog_keep_messages": 40,
    # 冷场主动（cold）要能自然提一句「前几天聊过的具体事」，留 80 条（约 40 轮、一天左右）。
    "cron_cold_keep_messages": 80,
    # 每轮对比上次请求，日志里指出前缀从第几条开始变
    "diagnose": True,
    # 打开后每轮在日志里打印省下的估算 token
    "verbose": False,
}

IMAGE_PLACEHOLDER = "[图片]"
GROUP_CONTEXT_PLACEHOLDER = "[群聊上下文已省略]"

# 群里每次请求都会在用户消息末尾塞一段现拉的群聊快照，这段只在当次有用，
# 历史里堆着会越来越大。以前这里只认 "BEGIN CONTEXT"，
# 而 group_proactive 实际发的标记是【本群真实最近消息】，结果一段都没被清过。
GROUP_CONTEXT_MARKS = ("BEGIN CONTEXT", "【本群真实最近消息】")

# AstrBot 每条用户消息末尾的提醒：
#   Current datetime: 2026-10-27 00:53 (CST), Weekday: Tuesday
# 只把这一行压成 [10-27 00:53]，身份信息原样保留。
DATETIME_RE = re.compile(
    r"Current datetime:\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})[^<\n]*"
)
# 2026-10-29 之前压成的是 @10-27 22:52。开头的 @ 和 QQ 的「@某人」是同一套写法，
# 群聊里容易被读成 @ 了一个叫「10-27 22:52」的人。这里重排成 [10-27 22:52]。
# 只认「行首 + 紧贴结尾标签」这种形态，且只在 system_reminder 文本里改，
# 不会碰到用户自己打的同类文本。
LEGACY_COMPACT_RE = re.compile(
    r"(?m)^@(\d{2}-\d{2} \d{2}:\d{2})(?=\s*</system_reminder>)"
)

# persona_growth 每轮注入的状态块（日记 + 脾气 + 此刻 + 最近说过的话 + 说话风格）。
# 这块里有「此刻：HH:MM」，而且因为放在 extra_user_content_parts 里会被永久写进
# 会话历史：一个群攒了 33 份互相矛盾的「此刻」，光这一块就占该群历史的 42%。
# 认块只认它固定的结尾（STYLE_OVERLAY 的最后一行）——用「此刻：」当标记会误伤
# 用户自己打的字。当前这一轮那份在 extra parts 里、不在 contexts 里，所以
# 历史里剩下的每一份都是过期的，全部换成占位符。
PERSONA_PLACEHOLDER = "[状态快照已省略]"
PERSONA_MARKS = ("不用硬套旧口头禅。",)


def _load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if not CONFIG_FILE.exists():
        return cfg
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"上下文瘦身：配置读取失败，使用默认值：{exc}")
        return cfg
    if isinstance(loaded, dict):
        cfg.update(loaded)
    return cfg


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _text_tokens(text: str) -> float:
    if not text:
        return 0.0
    zh = sum(1 for ch in text if "一" <= ch <= "鿿")
    return zh * 0.6 + (len(text) - zh) * 0.3


def _message_tokens(msg: dict[str, Any]) -> float:
    total = 0.0
    content = msg.get("content")
    if isinstance(content, str):
        total += _text_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                total += _text_tokens(part.get("text") or "")
            elif kind == "think":
                total += _text_tokens(part.get("think") or "")
            elif kind == "image_url":
                total += 765.0
    for call in msg.get("tool_calls") or []:
        total += _text_tokens(json.dumps(call, ensure_ascii=False))
    return total


def _is_empty_assistant(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "assistant" or msg.get("tool_calls"):
        return False
    content = msg.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text" and (part.get("text") or "").strip():
                return False
            if kind == "think" and (part.get("think") or "").strip():
                return False
            if kind in ("image_url", "audio_url"):
                return False
        return True
    return False


class Main(star.Star):
    """发送前整理历史消息，去掉每次请求都会重复携带的包袱。"""

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = _load_config()

    def _int(self, key: str, default: int, minimum: int = 0) -> int:
        return _as_int(self.cfg.get(key, default), default, minimum)

    # ---------- 各项清理 ----------

    def _strip_images(self, req: Any, contexts: list[dict]) -> int:
        keep = self._int("keep_recent_images", 2)
        # 只按配置保留，不再减掉「这次请求带的图」。
        # 一旦跟当前请求挂钩，同一条历史里的图就会在两次请求之间来回翻
        # （带图时全换成占位符、不带图时又留一张），
        # 图片那一格之后的整段前缀缓存全废。
        keep_in_history = max(0, keep)

        seen = 0
        removed = 0
        for msg in reversed(contexts):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in reversed(content):
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                if seen < keep_in_history:
                    seen += 1
                    continue
                part.clear()
                part.update({"type": "text", "text": IMAGE_PLACEHOLDER})
                removed += 1
        return removed

    def _clear_think(self, contexts: list[dict]) -> int:
        if not self.cfg.get("clear_historical_think", True):
            return 0
        cleared = 0
        for msg in contexts:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            kept: list[Any] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "think":
                    # 带签名的思考块不动，部分提供商要靠它还原上下文
                    if part.get("encrypted"):
                        kept.append(part)
                        continue
                    if part.get("think"):
                        cleared += 1
                    continue
                kept.append(part)
            msg["content"] = kept

        # 清空后只剩空壳的 assistant 消息直接丢掉
        contexts[:] = [m for m in contexts if not _is_empty_assistant(m)]
        return cleared

    def _shorten_tool_results(self, contexts: list[dict]) -> int:
        keep = self._int("keep_recent_tool_results", 5)
        max_chars = self._int("tool_result_max_chars", 100, minimum=20)
        indices = [i for i, m in enumerate(contexts) if m.get("role") == "tool"]
        if not indices:
            return 0
        if keep <= 0:
            targets = indices
        else:
            # 截断点按 trim_quantum 一格一格挪。每来一条工具结果就挪一格的话，
            # 历史中间那条会在「完整 / 截断」之间来回翻，
            # 它后面所有内容的缓存都会被作废（一次就是好几千 token）。
            quantum = self._int("trim_quantum", 10, minimum=1)
            extra = len(indices) - keep
            cut = 0 if extra <= 0 else (extra // quantum) * quantum
            targets = indices[:cut]

        changed = 0
        for idx in targets:
            msg = contexts[idx]
            content = msg.get("content")
            if isinstance(content, str):
                if len(content) > max_chars:
                    msg["content"] = content[:max_chars] + "…"
                    changed += 1
                continue
            if isinstance(content, list):
                texts = [
                    p.get("text") or ""
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if t)
                if len(joined) > max_chars:
                    msg["content"] = joined[:max_chars] + "…"
                    changed += 1
        return changed

    def _compact_reminders(self, contexts: list[dict]) -> int:
        if not self.cfg.get("compact_historical_reminders", True):
            return 0
        changed = 0
        for msg in contexts:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text") or ""
                if "<system_reminder>" not in text:
                    continue
                new_text = text
                if "Current datetime:" in new_text:
                    new_text = DATETIME_RE.sub(
                        lambda m: f"[{m.group(2)}-{m.group(3)} {m.group(4)}]",
                        new_text,
                    )
                new_text = LEGACY_COMPACT_RE.sub(
                    lambda m: f"[{m.group(1)}]",
                    new_text,
                )
                if new_text != text:
                    part["text"] = new_text
                    changed += 1
        return changed

    def _trim_persona_blocks(self, contexts: list[dict]) -> int:
        if not self.cfg.get("trim_persona_blocks", True):
            return 0
        removed = 0
        for msg in contexts:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = (part.get("text") or "").rstrip()
                if not text.endswith(PERSONA_MARKS):
                    continue
                part["text"] = PERSONA_PLACEHOLDER
                removed += 1
        return removed

    def _trim_group_contexts(self, contexts: list[dict]) -> int:
        keep = self._int("keep_recent_group_context_blocks", 2)
        blocks: list[dict] = []
        for msg in contexts:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and any(
                        mark in (part.get("text") or "")
                        for mark in GROUP_CONTEXT_MARKS
                    )
                ):
                    blocks.append(part)
        if not blocks:
            return 0
        if keep <= 0:
            targets = blocks
        else:
            # 同 _shorten_tool_results：截断点按粗粒度推进，不要每来一条新消息就整段进一格。
            # 每段快照有一两千字，所以这里用更小的步长（最多多留 2 段过期的）。
            quantum = self._int("block_trim_quantum", 3, minimum=1)
            extra = len(blocks) - keep
            cut = 0 if extra <= 0 else (extra // quantum) * quantum
            targets = blocks[:cut]
        for part in targets:
            part["text"] = GROUP_CONTEXT_PLACEHOLDER
        return len(targets)

    # ---------- 主动任务的历史裁剪 ----------

    @staticmethod
    def _cron_mode(event: Any) -> str:
        """取出这条请求属于哪类定时主动任务，普通聊天返回空串。"""
        try:
            payload = event.get_extra("cron_payload")
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(payload, dict):
            return str(payload.get("mode") or "")
        return ""

    def _trim_cron_contexts(self, event: Any, contexts: list[dict]) -> int:
        """补读/冷场这类一次性主动任务不必背整段历史。

        这两类任务的前缀天生是冷的（提示词是现场拼的，跟上一轮对不上），
        背 2 万多 token 的历史等于全额付费；而它们真正需要的信息
        （离线那几条、日记、承诺）已经写在任务提示词里了。
        watch / tail / followup 是在对话中途接话，必须留着完整上下文，不动。
        """
        limits = {
            "backlog": self._int("cron_backlog_keep_messages", 20),
            "cold": self._int("cron_cold_keep_messages", 80),
        }
        keep = limits.get(self._cron_mode(event))
        if not keep or len(contexts) <= keep:
            return 0

        removed = len(contexts) - keep
        del contexts[:-keep]

        # 不能从工具调用链 / 助手发言中间切开，开头残留的直接丢掉
        while contexts and contexts[0].get("role") != "user":
            contexts.pop(0)
        return removed

    # ---------- 诊断 ----------

    def _sig(self, msg: dict) -> tuple:
        """给一条消息算指纹：角色 + 长度 + 内容哈希。"""
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                (part.get("text") or part.get("think") or str(part.get("type") or ""))
                for part in content
                if isinstance(part, dict)
            )
        else:
            text = ""
        digest = hashlib.blake2b(text.encode("utf-8", "ignore"), digest_size=8)
        return (msg.get("role"), len(text), digest.hexdigest())

    def _diagnose(self, event: AstrMessageEvent, req: Any, contexts: list[dict]) -> None:
        """对比同一个会话上一次请求，指出前缀从第几条开始变了。

        前缀缓存只认「这次请求的开头跟上次一模一样」；只要中间有一条变了，
        它后面的内容就全部按全价重算。命中率异常时看这条日志就能定位。
        """
        key = ""
        try:
            key = str(event.unified_msg_origin or "")
        except Exception:
            key = ""
        if not key:
            return
        system_text = str(getattr(req, "system_prompt", "") or "")
        cur = [(("system", len(system_text), hashlib.blake2b(
            system_text.encode("utf-8", "ignore"), digest_size=8).hexdigest()))]
        cur.extend(self._sig(m) for m in contexts if isinstance(m, dict))

        store = getattr(self, "_last_sig", None)
        if store is None:
            store = {}
            self._last_sig = store
        last = store.get(key)
        store[key] = cur
        if not last:
            return
        limit = min(len(last), len(cur))
        for index in range(limit):
            if last[index] != cur[index]:
                logger.info(
                    "[缓存诊断] %s 前缀在第 %d 条起变化（上次 %d 条、这次 %d 条）：%s -> %s",
                    key[-20:],
                    index,
                    len(last),
                    len(cur),
                    last[index],
                    cur[index],
                )
                return

    # ---------- 入口 ----------

    @filter.on_llm_request()
    async def slim_context(self, event: AstrMessageEvent, req) -> None:
        if not self.cfg.get("enabled", True):
            return
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list) or not contexts:
            return

        before = sum(_message_tokens(m) for m in contexts if isinstance(m, dict))
        try:
            cron = self._trim_cron_contexts(event, contexts)
            images = self._strip_images(req, contexts)
            thinks = self._clear_think(contexts)
            tools = self._shorten_tool_results(contexts)
            reminders = self._compact_reminders(contexts)
            blocks = self._trim_group_contexts(contexts)
            personas = self._trim_persona_blocks(contexts)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"上下文瘦身失败，已跳过本次清理：{exc}")
            return

        # 主动任务的历史本来就是一次性的，跟上一轮对不上是设计如此，不必再报断点
        if self.cfg.get("diagnose", True) and not cron:
            try:
                self._diagnose(event, req, contexts)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"缓存前缀诊断失败：{exc}")

        after = sum(_message_tokens(m) for m in contexts if isinstance(m, dict))
        if self.cfg.get("verbose"):
            logger.info(
                "上下文瘦身：估算 token %d -> %d（主动任务 %d、图 %d、思考 %d、工具 %d、提醒 %d、群快照 %d、状态块 %d）",
                int(before),
                int(after),
                cron,
                images,
                thinks,
                tools,
                reminders,
                blocks,
                personas,
            )
