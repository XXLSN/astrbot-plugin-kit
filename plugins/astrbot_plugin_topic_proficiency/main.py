"""话题熟练度：她到底对某个话题熟不熟。

吃的是娱乐冲浪落下来的素材（persona_growth/surf_corpus/*.md），
按「接触过几次 / 每次挖多深 / 最近一次多久以前」算熟练度，
写成 proficiency.json 给群聊判定层用：熟的话题才敢多说两句。

只认实际看过的素材，不认聊天里别人提到过的词。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone
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

DEFAULT_ALIASES: dict[str, list[str]] = {
    "动画": ["番剧", "新番", "动漫", "动画片"],
    "画画": ["画图", "绘画", "手绘", "插画"],
    "游戏": ["手游", "端游", "打游戏", "独立游戏", "游戏实况"],
    "猫": ["猫咪", "小猫", "喵", "猫猫"],
    "编程": ["写代码", "代码", "编程语言"],
    "登山": ["爬山", "徒步"],
    "音乐": ["听歌", "歌曲", "唱歌", "音乐"],
    "美食": ["吃的", "零食", "好吃的", "吃播", "测评"],
    "宠物": ["狗", "狗狗", "养猫", "养狗"],
}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "corpus_dir": "",
    "cards_dir": "",
    "data_dir": "",
    "prefs_path": "",
    "knowledge_path": "",
    "knowledge_index": "",
    "kb_name": "",
    "kb_ingest_enabled": True,
    "kb_ingest_batch": 10,
    "poll_interval_seconds": 300,
    "startup_delay_seconds": 120,
    "recency_half_life_days": 30,
    "depth_budget": 3.0,
    "breadth_scale": 6.0,
    "min_sources_for_full": 2,
    "full_cap_below_min": 40.0,
    "topic_aliases": {},
}

DEEP_WORDS = ("研究", "搞懂", "查了", "玩过", "玩了很久", "看完", "看完了", "刷完", "通关")
MID_WORDS = ("看完", "看完了", "玩了", "试了", "看了一半")


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


def _parse_corpus(path: Path) -> dict[str, Any]:
    """拆 surf_corpus 的 markdown：头部的来源行、日记正文、素材正文。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    header, _, rest = text.partition("## 日记")
    diary, _, material = rest.partition("## 素材")
    sources: list[str] = []
    at = ""
    mode = ""
    deep = False
    for raw_line in header.splitlines():
        line = raw_line.strip()
        if line.startswith("- 时间:"):
            at = line.split(":", 1)[1].strip()
        elif line.startswith("- mode:"):
            mode = line.split(":", 1)[1].strip()
        elif line.startswith("- deep:"):
            deep = line.split(":", 1)[1].strip() in {"1", "true", "True"}
        elif line.startswith("- 来源:"):
            # 「- 来源:」本身是空壳，真正的来源缩进写在下面。
            rest = line[len("- 来源:"):].strip()
            if rest:
                sources.append(rest)
        elif line.startswith("- [") and raw_line[:1].isspace():
            sources.append(line[2:].strip())
    if mode and mode != "fun":
        return {}
    return {
        "key": path.name,
        "at": at,
        "sources": sources,
        "diary": diary.strip(),
        "material": material.strip(),
        "deep": deep,
        "text": text,
    }


def _score_source(record: dict[str, Any]) -> float:
    """一次接触挖得多深：整篇素材越完整越深，日记里有明确动作再抬一档。"""
    material_len = len(str(record.get("material") or ""))
    diary = str(record.get("diary") or "")
    if material_len >= 3500:
        depth = 1.0
    elif material_len >= 1200:
        depth = 0.7
    else:
        depth = 0.3
    if record.get("deep"):
        # 深读是抓正文 + 提炼知识卡，不是刷标题，按最深一档算。
        depth = 1.0
    elif any(word in diary for word in DEEP_WORDS):
        depth = max(depth, 1.0)
    elif any(word in diary for word in MID_WORDS):
        depth = max(depth, 0.7)
    return depth


class Main(star.Star):
    """把娱乐冲浪素材折算成话题熟练度。"""

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = self._load_config()
        self.data_dir = Path(str(self.cfg.get("data_dir") or _TOPIC_PROFICIENCY_DIR))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sources_file = self.data_dir / "sources.jsonl"
        self.output_file = self.data_dir / "proficiency.json"
        self.knowledge_file = Path(
            str(self.cfg.get("knowledge_path") or (_PERSONA_GROWTH_DIR / "knowledge.jsonl"))
        )
        if not self.cfg.get("corpus_dir"):
            self.cfg["corpus_dir"] = str(_PERSONA_GROWTH_DIR / "surf_corpus")
        if not self.cfg.get("cards_dir"):
            self.cfg["cards_dir"] = str(self.data_dir / "cards")
        if not self.cfg.get("prefs_path"):
            self.cfg["prefs_path"] = str(_INTEREST_PROFILE_DIR / "prefs.json")
        index_path = str(self.cfg.get("knowledge_index") or "").strip()
        self.knowledge_index = (
            Path(index_path) if index_path else self.data_dir / "knowledge_index.jsonl"
        )
        self._kb_warned = False

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(_load_json(CONFIG_FILE, dict))
        return cfg

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self.cfg.get("enabled", True):
            logger.info("话题熟练度已关闭（config.json enabled=false）")
            return
        old = getattr(self, "_loop_task", None)
        if isinstance(old, asyncio.Task) and not old.done():
            old.cancel()
        self._loop_task = asyncio.create_task(self._run_loop(), name="topic_proficiency_loop")
        logger.info(
            "话题熟练度已启动：素材目录 %s", self.cfg.get("corpus_dir"),
        )

    async def _run_loop(self) -> None:
        delay = int(self.cfg.get("startup_delay_seconds", 120) or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        interval = max(60, int(self.cfg.get("poll_interval_seconds", 300) or 300))
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[topic_proficiency] 轮询出错")
            await asyncio.sleep(interval)

    # ---------- 主流程 ----------

    async def _tick(self) -> None:
        # 知识卡入库和熟练度是两条独立流水线：别让「没有新素材」挡住入库。
        await self._ingest_knowledge()
        known = self._load_done_keys()
        files = self._collect_files()
        fresh = [path for path in files if path.name not in known]
        if not fresh:
            return
        aliases = self._aliases()
        added = 0
        for path in fresh:
            record = _parse_corpus(path)
            if not record:
                continue
            matched = self._match_topics(record["text"], aliases)
            depth = _score_source(record)
            _append_jsonl(
                self.sources_file,
                {
                    "key": record["key"],
                    "at": record["at"],
                    "sources": record["sources"][:6],
                    "matched": matched,
                    "depth": depth,
                    "material_len": len(record["material"]),
                },
            )
            added += 1
        if added:
            self._recompute()
            logger.info("[topic_proficiency] 新素材 %s 份，已重算熟练度", added)

    def _collect_files(self) -> list[Path]:
        out: list[Path] = []
        for key in ("corpus_dir", "cards_dir"):
            root = Path(str(self.cfg.get(key) or ""))
            if not str(root).strip() or not root.is_dir():
                continue
            out.extend(sorted(root.glob("*.md")))
        return out

    def _load_done_keys(self) -> set[str]:
        keys: set[str] = set()
        try:
            lines = self.sources_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return keys
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("key"):
                keys.add(str(item["key"]))
        return keys

    def _aliases(self) -> dict[str, list[str]]:
        aliases = {topic: list(words) for topic, words in DEFAULT_ALIASES.items()}
        for topic, words in (self.cfg.get("topic_aliases") or {}).items():
            if isinstance(words, str):
                words = [words]
            if isinstance(words, list):
                merged = aliases.setdefault(str(topic), [])
                for word in words:
                    word = str(word).strip()
                    if word and word not in merged:
                        merged.append(word)
        return aliases

    def _known_topics(self) -> list[str]:
        """能打分的只有她在意过的话题：prefs.json 里的 items，各算一条。"""
        prefs = _load_json(Path(str(self.cfg.get("prefs_path"))), dict)
        items = prefs.get("items") if isinstance(prefs, dict) else None
        topics: list[str] = []
        for item in items or []:
            if isinstance(item, dict) and item.get("topic"):
                topics.append(str(item["topic"]))
        return topics

    def _match_topics(self, text: str, aliases: dict[str, list[str]]) -> list[str]:
        haystack = text.casefold()
        matched: list[str] = []
        for topic in self._known_topics():
            names = [topic] + [w for w in aliases.get(topic, [])]
            if any(name.casefold() in haystack for name in names if name):
                matched.append(topic)
        return matched

    # ---------- 知识卡入库 ----------

    def _load_knowledge_cards(self) -> list[dict[str, Any]]:
        try:
            lines = self.knowledge_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        cards: list[dict[str, Any]] = []
        for line in lines[-1000:]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            if not item.get("id") or not item.get("topic"):
                continue
            facts = item.get("facts")
            if not isinstance(facts, list) or len(facts) < 2:
                continue
            cards.append(item)
        return cards

    def _load_ingested_ids(self) -> set[str]:
        try:
            lines = self.knowledge_index.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return set()
        done: set[str] = set()
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("id"):
                done.add(str(item["id"]))
        return done

    @staticmethod
    def _card_text(card: dict[str, Any]) -> str:
        """一张卡就是知识库里的一个 chunk，保留事实、原句和来源。"""
        lines = [f"主题：{str(card.get('topic') or '').strip()}"]
        for fact in card.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            text = str(fact.get("text") or "").strip()
            src = str(fact.get("src") or "").strip()
            if not text:
                continue
            lines.append(f"- {text}" + (f"（原文：{src}）" if src else ""))
        why = str(card.get("why") or "").strip()
        if why:
            lines.append(f"为什么有意思：{why}")
        quotes = [
            str(item).strip()
            for item in card.get("quotes") or []
            if str(item).strip()
        ]
        if quotes:
            lines.append("原文摘句：" + "｜".join(f"「{q}」" for q in quotes[:2]))
        source = card.get("source") if isinstance(card.get("source"), dict) else {}
        title = str(source.get("title") or "").strip()
        url = str(source.get("url") or "").strip()
        if title or url:
            lines.append(f"来源：{title} {url}".strip())
        return "\n".join(lines)[:1200]

    async def _ingest_knowledge(self) -> None:
        if not self.cfg.get("kb_ingest_enabled", True):
            return
        # 非向量路线：知识卡留给 persona_growth/group_proactive 本地读取，不入库。
        backend = str(self.cfg.get("knowledge_backend") or "vector").strip().lower()
        if backend == "local":
            return
        kb_name = str(self.cfg.get("kb_name") or "").strip()
        if not kb_name:
            return
        cards = self._load_knowledge_cards()
        if not cards:
            return
        done = self._load_ingested_ids()
        pending = [card for card in cards if str(card.get("id")) not in done]
        if not pending:
            return
        kb_mgr = getattr(self.context, "kb_manager", None)
        if kb_mgr is None:
            return
        kb = await kb_mgr.get_kb_by_name(kb_name)
        if kb is None:
            if not self._kb_warned:
                self._kb_warned = True
                logger.warning(
                    "[topic_proficiency] 知识库 %r 不存在，知识卡先攒着；"
                    "在 WebUI 建好同名知识库后会自动入库",
                    kb_name,
                )
            return
        self._kb_warned = False
        batch = max(1, int(self.cfg.get("kb_ingest_batch", 10) or 10))
        added = 0
        for card in pending[:batch]:
            card_id = str(card.get("id"))
            text = self._card_text(card)
            if not text.strip():
                continue
            try:
                doc = await kb.upload_document(
                    file_name=f"{card_id}.txt",
                    file_content=None,
                    file_type="txt",
                    pre_chunked_text=[text],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[topic_proficiency] 知识卡 %s 入库失败：%s", card_id, exc
                )
                continue
            _append_jsonl(
                self.knowledge_index,
                {
                    "id": card_id,
                    "doc_id": str(getattr(doc, "doc_id", "") or ""),
                    "topic": str(card.get("topic") or ""),
                    "kb_name": kb_name,
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
            )
            added += 1
        if added:
            logger.info("[topic_proficiency] 知识卡入库 %s 张 → %s", added, kb_name)

    # ---------- 计算 ----------

    def _load_sources(self) -> list[dict[str, Any]]:
        try:
            lines = self.sources_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-5000:]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("matched"):
                out.append(item)
        return out

    def _recompute(self) -> None:
        records = self._load_sources()
        if not records:
            return
        half_life = float(self.cfg.get("recency_half_life_days", 30) or 30)
        depth_budget = float(self.cfg.get("depth_budget", 3.0) or 3.0)
        breadth_scale = float(self.cfg.get("breadth_scale", 6.0) or 6.0)
        min_src = int(self.cfg.get("min_sources_for_full", 2) or 2)
        cap = float(self.cfg.get("full_cap_below_min", 40.0) or 0)
        now = datetime.now(timezone.utc)

        by_topic: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            for topic in record.get("matched") or []:
                by_topic.setdefault(str(topic), []).append(record)

        topics: dict[str, dict[str, Any]] = {}
        for topic, rows in by_topic.items():
            n_src = len(rows)
            depth_sum = sum(float(row.get("depth") or 0) for row in rows)
            breadth = 1.0 - math.exp(-n_src / max(1.0, breadth_scale))
            depth = min(1.0, depth_sum / max(1.0, depth_budget))
            newest = max(
                (self._parse_ts(str(row.get("at") or "")) for row in rows),
                default=None,
            )
            if newest is None:
                recency = 1.0
            else:
                days = max(0.0, (now - newest).total_seconds() / 86400)
                recency = 0.5 ** (days / max(1.0, half_life))
            prof = 100.0 * (0.55 * breadth + 0.30 * depth + 0.15 * recency)
            if n_src < min_src:
                prof = min(prof, cap)
            topics[topic] = {
                "prof": round(prof, 1),
                "n_src": n_src,
                "depth_sum": round(depth_sum, 2),
                "last": newest.isoformat(timespec="seconds") if newest else "",
                "breadth": round(breadth, 3),
                "depth": round(depth, 3),
                "recency": round(recency, 3),
            }

        payload = {
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "topics": topics,
        }
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.output_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _parse_ts(text: str) -> datetime | None:
        text = text.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
