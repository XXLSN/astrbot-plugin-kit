from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.agent.message import TextPart

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

CONFIG_FILE = Path(__file__).with_name("config.json")
MEMORY_FILE = Path(__file__).with_name("memories.json")
DEFAULT_CONFIG = {
    "data_dir": "",
    "max_notes": 2000,
    "max_note_len": 400,
    "pinned_char_budget": 600,
    "auto_recall_enabled": True,
    "auto_recall_max": 3,
    "auto_recall_char_budget": 900,
    "auto_recall_min_score": 2,
    "diary_recall_min_score": 4,
    "recall_max_results": 5,
    "recall_char_budget": 1200,
    "history_search_max_results": 5,
    "history_search_char_budget": 1200,
    "history_search_snippet_len": 240,
    "history_search_min_score": 2,
    "history_search_scan_limit": 2000,
    "diary_path": "",
    "key_facts_path": "",
}
DEFAULT_MAX_NOTES = 2000
DEFAULT_MAX_NOTE_LEN = 400
DEFAULT_MAX_KEY_FACTS = 5000
ALL_CONFIRM = "确认删除全部记忆"

# 长期记忆只存文字事实：图片占位符、图片链接、Markdown 图片都在入库前去掉。
IMAGE_MARKER_RE = re.compile(
    r"\[(?:图片|image|图片消息|表情包|cq:image[^\]]*)\]",
    re.IGNORECASE,
)
IMAGE_URL_RE = re.compile(
    r"https?://\S+?\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?\S*)?",
    re.IGNORECASE,
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
SCOPE_RE = re.compile(r"^(global|(?:private|group|friend):\d+)$")
MEMORY_CUE_RE = re.compile(r"记得|记不|上次|以前|之前|那次|说过|答应|忘了|回忆")

# 记忆读隔离：非管理员（含群聊）永远看不到夹带管理员称呼、
# 管理员 QQ、其他 QQ 号或隐私关键词的条目。
ADMIN_WORD_RE = re.compile(r"管理员账户|管理员账户|爹|管理员|主人|admin", re.IGNORECASE)
QQ_NUMBER_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
PRIVACY_WORD_RE = re.compile(
    r"电话|手机|住址|地址|单位|学校|行程|财务|密码|微信|身份证|银行卡"
    r"|api[_\s-]?key|token|secret",
    re.IGNORECASE,
)

STOP_TERMS = {
    "什么",
    "怎么",
    "记得",
    "记不",
    "以前",
    "之前",
    "那个",
    "这个",
    "事情",
    "时候",
    "现在",
    "我们",
    "刚才",
    "说的",
    "一个",
    "没有",
    "可以",
    "就是",
    "不是",
    "知道",
    "觉得",
    "然后",
    "还是",
    "比较",
    "真的",
    "已经",
    "你还",
    "还记",
    "今天",
    "昨天",
    "明天",
    "最近",
    "有点",
    "感觉",
    "一下",
    "一直",
    "其实",
    "可能",
    "应该",
}


def _query_terms(text: str) -> set[str]:
    """把查询切成中英文关键词片段，用于文件内搜索。"""
    text = str(text or "").casefold()
    terms: set[str] = set()
    for match in re.finditer(r"[a-z0-9_]{2,}", text):
        terms.add(match.group(0))
    for match in re.finditer(r"[\u4e00-\u9fff]+", text):
        chunk = match.group(0)
        for size in (2, 3):
            for i in range(0, len(chunk) - size + 1):
                term = chunk[i : i + size]
                if term not in STOP_TERMS:
                    terms.add(term)
    return terms


def _score(query: str, text: str) -> int:
    query = str(query or "").strip().casefold()
    text = str(text or "").casefold()
    if not query or not text:
        return 0
    if query in text:
        return 20 + min(len(query), 20)
    return sum(len(term) for term in _query_terms(query) if term in text)


class Main(star.Star):
    """自主记忆：短期聊天上下文由她自己清，长期记忆由她自己记和忘。

    文件可以存很多，但每次只把“钉住的少量事实”和“当前消息搜到的旧事”放进 prompt；
    其余靠 recall 工具按需搜索，避免记忆越多、每次 token 越高。
    """

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self._lock = asyncio.Lock()
        self.cfg = self._load_config()
        self._memory_file = self._resolve_memory_file()
        self._diary_path = self._resolve_path(
            self.cfg.get("diary_path"), str(_PERSONA_GROWTH_DIR / "diary.json")
        )
        self._key_facts_file = self._resolve_path(
            self.cfg.get("key_facts_path"), str(_SELF_MEMORY_DIR / "key_facts.json")
        )

    # ---------- 基础 ----------

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"长期记忆配置读取失败：{exc}")
        return cfg

    @staticmethod
    def _resolve_path(raw: Any, default: str) -> Path:
        text = str(raw or "").strip() or default
        return Path(os.path.expanduser(text))

    def _resolve_memory_file(self) -> Path:
        raw = str(self.cfg.get("data_dir") or "").strip()
        base = Path(os.path.expanduser(raw)) if raw else _SELF_MEMORY_DIR
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"记忆目录 {base} 不可用，回退到插件目录：{exc}")
            base = Path(__file__).parent
        target = base / "memories.json"
        if target != MEMORY_FILE and MEMORY_FILE.exists() and not target.exists():
            try:
                shutil.copy2(MEMORY_FILE, target)
                logger.info(f"已复制 memories.json 到 {base}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"迁移 memories.json 失败：{exc}")
        return target

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        return bool(checker()) if callable(checker) else False

    @staticmethod
    def _deny() -> str:
        return "error: 只有管理员可以管理记忆。"

    def _admin_ids(self) -> list[str]:
        try:
            ids = self.context.get_config().get("admins_id", [])
        except Exception:  # noqa: BLE001
            ids = []
        if not isinstance(ids, (list, tuple)):
            return []
        return [str(item).strip() for item in ids if str(item).strip()]

    def _private_target_id(self, event: AstrMessageEvent) -> str:
        """私聊看会话 id，不看 get_sender_id()。

        主动任务里 get_sender_id() 可能是建任务的管理员，但会话目标是
        普通好友；如果按发送人算，会把管理员账户的私聊记忆漏进好友的主动消息。
        """
        match = re.search(
            r"(?:FriendMessage|private|friend):(\d+)",
            str(getattr(event, "unified_msg_origin", "") or ""),
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _is_admin_private_event(self, event: AstrMessageEvent) -> bool:
        """只有「管理员本人私聊」才放开全部记忆；群聊一律按公开处理。"""
        try:
            if not event.is_private_chat():
                return False
        except Exception:  # noqa: BLE001
            return False
        target = self._private_target_id(event)
        return bool(target) and target in self._admin_ids()

    def _safe_for_non_admin(self, text: str) -> bool:
        raw = str(text or "")
        if not raw:
            return True
        if ADMIN_WORD_RE.search(raw):
            return False
        if any(uid and uid in raw for uid in self._admin_ids()):
            return False
        if QQ_NUMBER_RE.search(raw):
            return False
        if PRIVACY_WORD_RE.search(raw):
            return False
        return True

    @staticmethod
    def _clean_memory_text(text: str) -> str:
        """去掉图片占位符和图片链接，只留下文字事实。"""
        cleaned = MARKDOWN_IMAGE_RE.sub("", str(text or ""))
        cleaned = IMAGE_MARKER_RE.sub("", cleaned)
        cleaned = IMAGE_URL_RE.sub("", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _atomic_write(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _max_notes(self) -> int:
        try:
            value = int(self.cfg.get("max_notes", DEFAULT_MAX_NOTES) or DEFAULT_MAX_NOTES)
        except (TypeError, ValueError):
            value = DEFAULT_MAX_NOTES
        return max(50, min(20000, value))

    def _max_note_len(self) -> int:
        try:
            value = int(
                self.cfg.get("max_note_len", DEFAULT_MAX_NOTE_LEN)
                or DEFAULT_MAX_NOTE_LEN
            )
        except (TypeError, ValueError):
            value = DEFAULT_MAX_NOTE_LEN
        return max(80, min(1000, value))

    def _max_key_facts(self) -> int:
        try:
            value = int(
                self.cfg.get("max_key_facts", DEFAULT_MAX_KEY_FACTS)
                or DEFAULT_MAX_KEY_FACTS
            )
        except (TypeError, ValueError):
            value = DEFAULT_MAX_KEY_FACTS
        return max(50, min(20000, value))

    # ---------- 作用域 ----------

    def _current_scope(self, event: AstrMessageEvent) -> str:
        try:
            if event.is_private_chat():
                target = self._private_target_id(event)
                return f"private:{target}" if target else "global"
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            match = re.search(r"GroupMessage:(\d+)", umo)
            if match:
                return f"group:{match.group(1)}"
        except Exception:  # noqa: BLE001
            pass
        return "global"

    @staticmethod
    def _scope_visible(scope: str, current: str) -> bool:
        scope = str(scope or "").strip()
        return not scope or scope == "global" or scope == current

    def _infer_legacy_scope(self, text: str) -> str:
        """老记忆没有 scope 时按内容补：提到管理员账户的默认只给管理员账户私聊看。"""
        low = str(text or "").casefold()
        if any(word in low for word in ("管理员账户", "爹", "管理员", "admin")):
            ids = self._admin_ids()
            if ids:
                return f"private:{ids[0]}"
        return "global"

    # ---------- 记忆文件 ----------

    def _load_notes(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._memory_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.warning("长期记忆文件读取失败，本次按空记忆处理。")
            return []
        if not isinstance(raw, list):
            return []
        notes: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = self._clean_memory_text(str(item.get("text") or ""))
            if not text:
                continue
            try:
                note_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            try:
                importance = int(item.get("importance", 2))
            except (TypeError, ValueError):
                importance = 2
            notes.append(
                {
                    "id": note_id,
                    "text": text[: self._max_note_len()],
                    "scope": str(item.get("scope") or "").strip()
                    or self._infer_legacy_scope(text),
                    "importance": max(1, min(3, importance)),
                    "created_at": str(item.get("created_at") or ""),
                    "source": str(item.get("source") or "manual"),
                }
            )
        notes.sort(key=lambda note: note["id"])
        return notes[-self._max_notes() :]

    def _save_notes(self, notes: list[dict[str, Any]]) -> None:
        self._atomic_write(self._memory_file, notes[-self._max_notes() :])

    @staticmethod
    def _next_id(notes: list[dict[str, Any]]) -> int:
        return max((int(note.get("id", 0)) for note in notes), default=0) + 1

    def _load_key_facts(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._key_facts_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.warning("关键事文件读取失败，本次按空处理。")
            return []
        if not isinstance(raw, list):
            return []
        facts: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = self._clean_memory_text(str(item.get("text") or ""))
            if not text:
                continue
            try:
                fact_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            try:
                importance = int(item.get("importance", 2))
            except (TypeError, ValueError):
                importance = 2
            facts.append(
                {
                    "id": fact_id,
                    "text": text[: self._max_note_len()],
                    "scope": str(item.get("scope") or "global").strip() or "global",
                    "importance": max(1, min(3, importance)),
                    "created_at": str(item.get("created_at") or ""),
                    "source": str(item.get("source") or "auto"),
                }
            )
        facts.sort(key=lambda fact: fact["id"])
        return facts[-self._max_key_facts() :]

    def _save_key_facts(self, facts: list[dict[str, Any]]) -> None:
        self._atomic_write(self._key_facts_file, facts[-self._max_key_facts() :])

    def _load_diary(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._diary_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.warning("日记文件读取失败，本次按空处理。")
            return []
        if not isinstance(raw, list):
            return []
        entries: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = self._clean_memory_text(str(item.get("text") or ""))
            if not text:
                continue
            entries.append(
                {
                    "text": text,
                    "at": str(item.get("at") or ""),
                    "scope": str(item.get("scope") or "web"),
                }
            )
        return entries

    def _diary_visible(self, item: dict[str, Any], event: AstrMessageEvent) -> bool:
        scope = str(item.get("scope") or "web")
        if self._is_admin_private_event(event):
            return True
        # 群聊和普通好友私聊都只能看「不含管理员/隐私」的日记。
        # 以前群聊直接放行 web/group 日记，导致一堆提到「管理员账户」的日记被
        # 注入到公开群里；这里先过内容闸门，再看作用域。
        if not self._safe_for_non_admin(str(item.get("text") or "")):
            return False
        try:
            if event.is_private_chat():
                return scope == "web"
        except Exception:  # noqa: BLE001
            pass
        return scope in ("group", "web")

    # ---------- 可见集合与搜索 ----------

    def _visible_notes(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        current = self._current_scope(event)
        admin_private = self._is_admin_private_event(event)
        return [
            note
            for note in self._load_notes()
            if self._scope_visible(str(note.get("scope") or ""), current)
            and (
                admin_private
                or self._safe_for_non_admin(str(note.get("text") or ""))
            )
        ]

    def _visible_key_facts(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        current = self._current_scope(event)
        admin_private = self._is_admin_private_event(event)
        return [
            fact
            for fact in self._load_key_facts()
            if self._scope_visible(str(fact.get("scope") or ""), current)
            and (
                admin_private
                or self._safe_for_non_admin(str(fact.get("text") or ""))
            )
        ]

    @staticmethod
    def _search_items(
        query: str, items: list[dict[str, Any]], limit: int, min_score: int
    ) -> list[dict[str, Any]]:
        scored: list[tuple[int, dict[str, Any]]] = []
        seen: set[str] = set()
        for item in items:
            text = str(item.get("text") or "")
            key = re.sub(r"\s+", "", text).casefold()
            if not key or key in seen:
                continue
            score = _score(query, text)
            if score < min_score:
                continue
            seen.add(key)
            item["_score"] = score
            scored.append((score, item))
        scored.sort(
            key=lambda pair: (
                pair[0],
                str(pair[1].get("created_at") or pair[1].get("at") or ""),
            ),
            reverse=True,
        )
        return [item for _, item in scored[: max(1, limit)]]

    def _recall_items(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int,
        exclude: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        fact_items: list[dict[str, Any]] = []
        for note in self._visible_notes(event):
            fact_items.append({**note, "_kind": "记忆"})
        for fact in self._visible_key_facts(event):
            fact_items.append({**fact, "_kind": "关键事"})
        diary_items: list[dict[str, Any]] = []
        for entry in self._load_diary():
            if self._diary_visible(entry, event):
                diary_items.append({**entry, "_kind": "日记"})

        min_score = int(self.cfg.get("auto_recall_min_score", 2) or 2)
        diary_min_score = int(self.cfg.get("diary_recall_min_score", 4) or 4)
        results = self._search_items(query, fact_items, limit, min_score)
        if len(results) < limit:
            results += self._search_items(
                query, diary_items, limit - len(results), diary_min_score
            )

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        results.sort(
            key=lambda item: (
                int(item.get("_score", 0)),
                str(item.get("created_at") or item.get("at") or ""),
            ),
            reverse=True,
        )
        for item in results:
            key = re.sub(r"\s+", "", str(item.get("text") or "")).casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        results = merged[:limit]
        if exclude:
            results = [
                item
                for item in results
                if re.sub(r"\s+", "", str(item.get("text") or "")).casefold()
                not in exclude
            ]
        return results

    def _pinned_items(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [
            note for note in self._visible_notes(event) if note["importance"] >= 3
        ]
        items.extend(
            fact for fact in self._visible_key_facts(event) if fact["importance"] >= 3
        )
        items.sort(
            key=lambda item: (int(item.get("importance", 2)), int(item.get("id", 0))),
            reverse=True,
        )
        budget = int(self.cfg.get("pinned_char_budget", 600) or 600)
        picked: list[dict[str, Any]] = []
        used = 0
        for item in items:
            line = f"- {item['text']}"
            if picked and used + len(line) > budget:
                break
            picked.append(item)
            used += len(line)
        return picked

    def _recent_items(
        self, event: AstrMessageEvent, limit: int
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for note in self._visible_notes(event):
            if note["importance"] >= 2:
                items.append({**note, "_kind": "记忆"})
        for fact in self._visible_key_facts(event):
            if fact["importance"] >= 2:
                items.append({**fact, "_kind": "关键事"})
        items.sort(
            key=lambda item: (str(item.get("created_at") or ""), int(item.get("id", 0))),
            reverse=True,
        )
        return items[: max(1, limit)]

    @staticmethod
    def _format_recall(items: list[dict[str, Any]]) -> str:
        if not items:
            return ""
        lines = [
            "【可能相关的旧记忆】下面是从你自己的记忆文件里搜到的旧事。"
            "只在相关时自然使用，不要逐条复述；不相关就忽略。"
        ]
        for item in items:
            date = str(item.get("created_at") or item.get("at") or "")[:10]
            kind = str(item.get("_kind") or "记忆")
            prefix = f"- {date}｜{kind}｜" if date else f"- {kind}｜"
            lines.append(prefix + str(item.get("text") or ""))
        return "\n".join(lines)

    # ---------- 对话状态 ----------

    async def _clear_current_conversation(self, event: AstrMessageEvent) -> bool:
        umo = event.unified_msg_origin
        manager = self.context.conversation_manager
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return False
            await manager.update_conversation(umo, cid, history=[])
            return True
        except Exception:  # noqa: BLE001
            logger.exception("清空聊天上下文失败。")
            return False

    async def _conversation_info(self, event: AstrMessageEvent) -> tuple[str, int]:
        umo = event.unified_msg_origin
        manager = self.context.conversation_manager
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return "", 0
            conv = await manager.get_conversation(umo, cid)
            if conv is None:
                return cid, 0
            history = conv.history
            if isinstance(history, str):
                try:
                    history = json.loads(history)
                except Exception:  # noqa: BLE001
                    history = []
            if not isinstance(history, list):
                history = []
            return cid, len(history)
        except Exception:  # noqa: BLE001
            logger.exception("读取会话状态失败。")
            return "", 0

    # ---------- 旧聊天记录搜索 ----------

    @staticmethod
    def _strip_history_text(text: str) -> str:
        text = re.sub(
            r"<system_reminder>.*?</system_reminder>",
            " ",
            str(text or ""),
            flags=re.S | re.I,
        )
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return " ".join(parts)
        return ""

    async def _history_messages(self, event: AstrMessageEvent) -> list[Any]:
        """读取当前会话的完整聊天记录（数据库里保留的，不受轮数截断影响）。"""
        umo = event.unified_msg_origin
        manager = self.context.conversation_manager
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await manager.get_conversation(umo, cid)
            if conv is None:
                return []
            history = conv.history
            if isinstance(history, str):
                history = json.loads(history)
            return history if isinstance(history, list) else []
        except Exception:  # noqa: BLE001
            logger.exception("读取旧聊天记录失败。")
            return []

    @staticmethod
    def _snippet(text: str, query: str, width: int) -> str:
        if len(text) <= width:
            return text
        lowered = text.casefold()
        positions = [
            lowered.find(term)
            for term in _query_terms(query)
            if term and lowered.find(term) >= 0
        ]
        if query and query.casefold() in lowered:
            positions.append(lowered.find(query.casefold()))
        center = min(positions) if positions else 0
        start = max(0, center - width // 3)
        end = min(len(text), start + width)
        start = max(0, end - width)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end]}{suffix}"

    @staticmethod
    def _platform_message_text(content: Any) -> str:
        if not isinstance(content, dict):
            return ""
        parts: list[str] = []
        for part in content.get("message") or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "plain":
                parts.append(str(part.get("text") or ""))
            elif kind == "at":
                name = str(part.get("name") or part.get("user_id") or "").strip()
                if name:
                    parts.append(f"@{name}")
            elif kind == "reply":
                sender = str(part.get("sender_name") or "").strip()
                text = str(part.get("text") or "").strip()
                if sender or text:
                    parts.append(f"[回复{sender}：{text}]")
        return " ".join(parts)

    async def _platform_history_messages(
        self, event: AstrMessageEvent
    ) -> list[Any]:
        """读群消息历史表：群友说的话都在这儿，每群只保留最近 N 条。"""
        try:
            message_type = getattr(
                event.get_message_type(), "value", event.get_message_type()
            )
        except Exception:  # noqa: BLE001
            return []
        if str(message_type).lower() != "groupmessage":
            return []
        manager = getattr(self.context, "message_history_manager", None)
        if manager is None:
            return []
        limit = int(self.cfg.get("history_search_scan_limit", 2000) or 2000)
        try:
            return await manager.get(
                event.get_platform_id(),
                event.unified_msg_origin,
                page=1,
                page_size=max(1, min(limit, 5000)),
            )
        except Exception:  # noqa: BLE001
            logger.exception("读取群消息历史失败。")
            return []

    async def _search_history(
        self, event: AstrMessageEvent, query: str, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        min_score = int(self.cfg.get("history_search_min_score", 2) or 2)
        snippet_len = int(self.cfg.get("history_search_snippet_len", 240) or 240)
        candidates: list[tuple[int, int, dict[str, Any]]] = []

        messages = await self._history_messages(event)
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            text = self._strip_history_text(self._message_text(message))
            if not text:
                continue
            score = _score(query, text)
            if score < min_score:
                continue
            role = "我" if message.get("role") == "assistant" else "对方"
            candidates.append(
                (
                    score,
                    index,
                    {"text": text, "label": f"上下文第{index + 1}条｜{role}"},
                )
            )

        platform_rows = await self._platform_history_messages(event)
        for index, row in enumerate(platform_rows):
            text = self._strip_history_text(
                self._platform_message_text(getattr(row, "content", None))
            )
            if not text:
                continue
            score = _score(query, text)
            if score < min_score:
                continue
            sender = str(getattr(row, "sender_name", "") or "").strip()
            if not sender or sender.lower() == "bot":
                sender = "我"
            when = str(getattr(row, "created_at", "") or "")[5:16]
            label = f"{when}｜{sender}" if when else sender
            candidates.append((score, index, {"text": text, "label": label}))

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _score_value, _index, item in candidates:
            snippet = self._snippet(item["text"], query, snippet_len)
            if snippet in seen:
                continue
            seen.add(snippet)
            results.append({**item, "text": snippet})
            if len(results) >= limit:
                break
        total = len(messages) + len(platform_rows)
        return results, total

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="memory_status")
    async def memory_status(self, event: AstrMessageEvent) -> str:
        """查看自己现在的记忆状态：当前聊天上下文有多少条消息、长期记忆有多少条、哪些是常驻的。仅管理员可用。用户问「你还记得什么」「你现在记得多少」时用本工具。"""
        if not self._is_admin(event):
            return self._deny()
        cid, count = await self._conversation_info(event)
        notes = self._load_notes()
        facts = self._load_key_facts()
        visible_notes = self._visible_notes(event)
        visible_facts = self._visible_key_facts(event)
        pinned = self._pinned_items(event)
        message_type = getattr(
            event.get_message_type(), "value", event.get_message_type()
        )
        is_group = str(message_type).lower() == "groupmessage"
        lines = [
            f"会话类型：{'群聊' if is_group else '私聊'}",
            f"当前作用域：{self._current_scope(event)}",
            f"当前对话：{cid[:8] + '…' if cid else '（还没有对话）'}",
            f"短期上下文：{count} 条消息（这部分说清空就能清空）",
            f"长期记忆：手动 {len(notes)} 条，自动关键事 {len(facts)} 条；"
            f"当前会话可见 {len(visible_notes) + len(visible_facts)} 条，"
            f"常驻 {len(pinned)} 条。",
        ]
        recent = visible_notes[-3:] + visible_facts[-3:]
        if recent:
            lines.append(
                "最近记的是："
                + "；".join(f"#{note['id']} {note['text']}" for note in recent[-5:])
            )
        else:
            lines.append("长期记忆还是空的，遇到值得记的事可以用 remember 记下来。")
        return "\n".join(lines)

    @filter.llm_tool(name="clear_chat_memory")
    async def clear_chat_memory(self, event: AstrMessageEvent) -> str:
        """清空当前会话的聊天上下文（等同于 /reset），但保留长期记忆。仅管理员可用。用户说「忘了刚才聊的」「清空上下文」「重新开始」时用本工具。清空会在你这句回复发出去之后最终生效。"""
        if not self._is_admin(event):
            return self._deny()
        await self._clear_current_conversation(event)
        event.set_extra("_self_memory_clear_after", True)
        event.set_extra("_clean_group_context_session", True)
        return "好，之前的聊天上下文已经清掉了，长期记忆没动。"

    @filter.llm_tool(name="remember")
    async def remember(
        self,
        event: AstrMessageEvent,
        text: str,
        importance: int,
        scope: str,
    ) -> str:
        """把一件值得长期记住的事写进长期记忆，清空上下文或重启后仍然记得。仅管理员可用。遇到管理员账户的偏好、忌讳、约定、重要日期、近况等以后还会用到的信息时，主动调用本工具，不要等用户提醒。只记文字事实：不要记图片、表情包、截图或对图片的描述，也不要记图片链接；用户只发图片时不要调用本工具。

        Args:
            text(string): 要记住的事，一句话说清楚，不超过 400 字。不要记密码、API Key、身份证号等敏感信息。
            importance(number): 重要程度，1 到 3。过敏、称呼、明确边界、重要约定用 3（会一直放进 prompt）；普通偏好、计划、近况用 2；次要信息用 1。
            scope(string): 作用域。只给当前会话看就传当前会话；到处都适用才传 global；也可以传 private:QQ号、group:群号、friend:QQ号。不确定时传当前会话。
        """
        if not self._is_admin(event):
            return self._deny()
        raw_body = str(text or "").strip()
        body = self._clean_memory_text(raw_body)
        if not body:
            return "图片和图片链接不写进长期记忆。要记的话，请用文字把事实说清楚。"
        body = body[: self._max_note_len()]
        try:
            level = int(importance)
        except (TypeError, ValueError):
            level = 2
        level = max(1, min(3, level))
        current = self._current_scope(event)
        requested = str(scope or "").strip()
        final_scope = requested if SCOPE_RE.match(requested) else current
        if final_scope.startswith("private:") and not self._is_admin(event):
            final_scope = current
        async with self._lock:
            notes = self._load_notes()
            for note in notes:
                if (
                    note["scope"] == final_scope
                    and note["text"].casefold() == body.casefold()
                ):
                    return f"这件事已经记过了（#{note['id']}），没有重复保存。"
            if len(notes) >= self._max_notes():
                return (
                    f"error: 长期记忆已经有 {len(notes)} 条，先删掉几条不重要的再记。"
                )
            note = {
                "id": self._next_id(notes),
                "text": body,
                "scope": final_scope,
                "importance": level,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source": "manual",
            }
            notes.append(note)
            self._save_notes(notes)
        suffix = "（图片部分已忽略）" if raw_body != body else ""
        return (
            f"记住了（#{note['id']}，{final_scope}，重要度 {level}）：{body}{suffix}"
        )

    @filter.llm_tool(name="recall")
    async def recall(self, event: AstrMessageEvent, query: str) -> str:
        """从自己的长期记忆文件、关键事文件和日记里搜索旧事。用户提到以前的事、你感觉记不清、或者需要确认某个细节时调用。这里没搜到、又想找原话时，改用 search_history 翻旧聊天记录。

        Args:
            query(string): 搜索关键词，比如「面试」「白饭」「花生」「上次答应的事」。用具体名词，不要用「那个事」这种空词。
        """
        query = str(query or "").strip()
        if not query:
            return "error: 要搜什么？给个关键词。"
        limit = int(self.cfg.get("recall_max_results", 5) or 5)
        items = self._recall_items(event, query, limit)
        if not items:
            return "没有找到相关的旧记忆。"
        text = self._format_recall(items)
        budget = int(self.cfg.get("recall_char_budget", 1200) or 1200)
        return text[:budget]

    @filter.llm_tool(name="search_history")
    async def search_history(self, event: AstrMessageEvent, query: str) -> str:
        """在当前会话的完整旧聊天记录里搜原话。聊天上下文只带最近 200 轮，更早的原话会被挤出去，但本地记录还留着，用本工具能翻出来。群聊时还会搜群友最近 2000 条发言。用户提到很久以前聊过的事、你感觉没有这段记忆、或者 recall 没搜到时调用。

        Args:
            query(string): 搜索关键词，用具体名词或短句，比如「花生」「周三面试」「上次说的那个游戏」。不要用「那个事」这种空词。
        """
        query = str(query or "").strip()
        if not query:
            return "error: 要搜什么？给个关键词。"
        limit = int(self.cfg.get("history_search_max_results", 5) or 5)
        items, total = await self._search_history(event, query, limit)
        if not items:
            return "当前会话的旧聊天记录里也没搜到相关原话。"
        lines = [f"从当前会话的 {total} 条本地记录里翻到："]
        for item in items:
            lines.append(f"- {item['label']}：{item['text']}")
        budget = int(self.cfg.get("history_search_char_budget", 1200) or 1200)
        return "\n".join(lines)[:budget]

    @filter.llm_tool(name="list_memories")
    async def list_memories(self, event: AstrMessageEvent) -> str:
        """列出当前会话能看到的长期记忆和自动关键事，最多 20 条。用户问「你都记得我什么」时用本工具；想找具体某件事用 recall。"""
        notes = [
            {**note, "_kind": "记忆"} for note in self._visible_notes(event)
        ]
        facts = [
            {**fact, "_kind": "关键事"} for fact in self._visible_key_facts(event)
        ]
        items = notes + facts
        if not items:
            return "长期记忆还是空的。"
        items.sort(
            key=lambda item: (str(item.get("created_at") or ""), int(item.get("id", 0))),
            reverse=True,
        )
        lines = [f"当前会话可见的长期记忆一共 {len(items)} 条（最多显示 20 条）："]
        for item in items[:20]:
            date = str(item.get("created_at") or "")[:10]
            lines.append(
                f"#{item['id']}｜{item['_kind']}｜{item['scope']}｜"
                f"{item['text']}｜{date}"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="forget_memory")
    async def forget_memory(self, event: AstrMessageEvent, target: str) -> str:
        """删除一条长期记忆或自动关键事。仅管理员可用。用户说「忘掉关于 X 的事」时用本工具。

        Args:
            target(string): 记忆编号（纯数字，或 M3 / F2 这种带前缀的编号）或能唯一对上的关键词/原句。
        """
        if not self._is_admin(event):
            return self._deny()
        query = str(target or "").strip()
        if not query:
            return "error: 要删哪一条？给个编号或关键词。"
        async with self._lock:
            notes = self._load_notes()
            facts = self._load_key_facts()
            matched: list[tuple[str, dict[str, Any]]] = []
            upper = query.upper()
            if upper.startswith("M") and upper[1:].isdigit():
                matched = [
                    ("note", note)
                    for note in notes
                    if note["id"] == int(upper[1:])
                ]
            elif upper.startswith("F") and upper[1:].isdigit():
                matched = [
                    ("fact", fact)
                    for fact in facts
                    if fact["id"] == int(upper[1:])
                ]
            elif query.isdigit():
                matched = [
                    ("note", note) for note in notes if note["id"] == int(query)
                ]
                if not matched:
                    matched = [
                        ("fact", fact)
                        for fact in facts
                        if fact["id"] == int(query)
                    ]
            if not matched:
                exact_notes = [
                    ("note", note)
                    for note in notes
                    if note["text"].casefold() == query.casefold()
                ]
                exact_facts = [
                    ("fact", fact)
                    for fact in facts
                    if fact["text"].casefold() == query.casefold()
                ]
                matched = exact_notes + exact_facts
            if not matched:
                matched = [
                    ("note", note)
                    for note in notes
                    if query.casefold() in note["text"].casefold()
                ] + [
                    ("fact", fact)
                    for fact in facts
                    if query.casefold() in fact["text"].casefold()
                ]
            if not matched:
                return f"没有找到和「{query}」对得上的记忆。"
            if len(matched) > 1:
                lines = [f"「{query}」能对上 {len(matched)} 条，请用编号指定："]
                for kind, item in matched[:20]:
                    prefix = "M" if kind == "note" else "F"
                    lines.append(f"{prefix}#{item['id']}｜{item['text']}")
                return "\n".join(lines)
            kind, item = matched[0]
            if kind == "note":
                notes = [note for note in notes if note["id"] != item["id"]]
                self._save_notes(notes)
                return f"已经忘掉了（M#{item['id']}）：{item['text']}"
            facts = [fact for fact in facts if fact["id"] != item["id"]]
            self._save_key_facts(facts)
            return f"已经忘掉了（F#{item['id']}）：{item['text']}"

    @filter.llm_tool(name="clear_long_term_memory")
    async def clear_long_term_memory(
        self, event: AstrMessageEvent, confirm: str
    ) -> str:
        """删除全部长期记忆和自动关键事，不可恢复。仅管理员可用。只有用户明确要求「把长期记忆全删了」时才调用。

        Args:
            confirm(string): 必须原样传入「确认删除全部记忆」，否则不会执行。
        """
        if not self._is_admin(event):
            return self._deny()
        if str(confirm or "").strip() != ALL_CONFIRM:
            return (
                "没有删除。要清空全部长期记忆，需要用户明确同意，"
                f"并把 confirm 原样传成「{ALL_CONFIRM}」。"
            )
        async with self._lock:
            self._save_notes([])
            self._save_key_facts([])
        return "全部长期记忆已经清空了。"

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject_memories(self, event: AstrMessageEvent, req) -> None:
        if event.get_extra("_self_memory_injected", False):
            return
        event.set_extra("_self_memory_injected", True)

        pinned = self._pinned_items(event)
        if pinned:
            lines = [
                "",
                "【一定要记住的事】下面是你自己记下的长期事实，和当前聊天相关时自然使用，不要生硬复述。",
            ]
            lines.extend(f"- {item['text']}" for item in pinned)
            req.system_prompt = (req.system_prompt or "") + "\n".join(lines)

        if not self.cfg.get("auto_recall_enabled", True):
            return
        query = str(getattr(event, "message_str", "") or "").strip()
        if not query:
            return
        pinned_texts = {
            re.sub(r"\s+", "", str(item.get("text") or "")).casefold()
            for item in pinned
        }
        limit = int(self.cfg.get("auto_recall_max", 3) or 3)
        items = self._recall_items(event, query, limit, exclude=pinned_texts)
        if not items and MEMORY_CUE_RE.search(query):
            items = self._recent_items(event, limit)
        if not items:
            if MEMORY_CUE_RE.search(query):
                if getattr(req, "extra_user_content_parts", None) is None:
                    req.extra_user_content_parts = []
                req.extra_user_content_parts.append(
                    TextPart(
                        text="【记忆提示】长期记忆文件里没搜到这段。"
                        "如果用户指的是更早的聊天，可以用 search_history 翻当前会话的完整旧记录。"
                    ).mark_as_temp()
                )
            return
        text = self._format_recall(items)
        budget = int(self.cfg.get("auto_recall_char_budget", 900) or 900)
        text = text[:budget]
        if not text:
            return
        if getattr(req, "extra_user_content_parts", None) is None:
            req.extra_user_content_parts = []
        req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())

    # ---------- 延迟清空 ----------

    @filter.after_message_sent()
    async def clear_after_send(self, event: AstrMessageEvent) -> None:
        if not event.get_extra("_self_memory_clear_after", False):
            return
        await self._clear_current_conversation(event)


__all__ = ["Main"]
