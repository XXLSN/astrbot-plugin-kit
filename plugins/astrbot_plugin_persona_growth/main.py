from __future__ import annotations

import asyncio
import html
import json
import os
import random
import re
import shutil
import sqlite3
import time
import zlib
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain

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
META_FILE = Path(__file__).with_name("meta.json")
DIARY_FILE = Path(__file__).with_name("diary.json")
TURNS_FILE = Path(__file__).with_name("turns.jsonl")

MAX_TURNS = 400
MAX_DIARY_FALLBACK = 2000
MAX_DIARY_HARD_CAP = 10000
MAX_KEY_FACTS_FALLBACK = 5000
MAX_KEY_FACTS_HARD_CAP = 20000

# 傲/娇的动态比例。和爸聊天固定五五开，不在这里管；
# 其他人默认五成傲，被怼会往七成傲漂，被夸会往三成傲（七成娇）漂。
# 「傲」= 嘴硬别扭，不等于凶：只在刚被惹到时轻轻回一句，见 MOOD_LINE_TEMPLATE。
MOOD_BASE = 0.5
MOOD_MIN = 0.3
MOOD_MAX = 0.7
MOOD_PROVOKE_STEP = 0.08
MOOD_PRAISE_STEP = 0.08
MOOD_RELAX_PER_MESSAGE = 0.04  # 每说一轮往基线回一点，不然一次吵架会永远记着
MOOD_RELAX_FULL_HOURS = 4.0  # 完全没聊时，大约这么久回到基线

# 只在明确冲她说的句子里找这些词，免得把群友之间的互骂算到她头上。
MOOD_PROVOKE_RE = re.compile(
    r"(?<!不)傻|笨|蠢|弱智|有病|废物|垃圾|闭嘴|滚开|滚啊|去死|烦死|"
    r"讨厌你|你算(?:什么|个)|你懂什么|别(?:烦|叫|吵)|菜(?:死|鸡)|闭嘴"
)
MOOD_PRAISE_RE = re.compile(
    r"(?<![不没])可爱|(?<![不没])好听|(?<![不没])喜欢你|(?<![不没])爱你|"
    r"(?<![不没])厉害|(?<![不没])好棒|(?<![不没])聪明|(?<![不没])温柔|"
    r"(?<![不没])贴心|(?<![不没])真乖|(?<![不没])萌"
)

MOOD_MENTION_KEYWORDS = [
    "糯糯", "小糯", "糯糯酱", "糯酱", "糯宝", "阿糯",
    "糯米", "糯叽叽", "糯团", "小糯米",
]

MOOD_LINE_TEMPLATE = (
    "【此刻的脾气】大概 {hard} 成傲、{soft} 成娇。"
    "这里的傲是嘴硬、别扭、不肯先服软，不是凶：不许主动怼人，不阴阳怪气，"
    "不吐槽、不评价别人的喜好和选择，不逮着一个人连着刺。"
    "只有对方刚骂你、嘲你、使唤你，才可以轻轻回一句，然后收住。"
)

STYLE_QUERIES = [
    "傲娇 温柔 台词",
    "傲娇 嘴硬心软 对话",
    "傲娇 关心人 台词",
    "傲娇 害羞 反差 说话方式",
]

# 「娇」那一半的素材：傲娇只学傲，越学越凶，所以要一半时间专门学软。
SOFT_STYLE_QUERIES = [
    "撒娇 台词 片段",
    "害羞 小说 对话",
    "软萌 说话方式",
    "关心人 温柔 台词",
]

CURIOSITY_TOPICS = [
    "最近有趣的小动物新闻",
    "最近好玩的独立游戏",
    "最近新出的甜点或零食",
    "最近流行的网络梗",
    "最近好看的动画或漫画",
    "适合雨天听的歌",
    "可爱的手作或周边",
    "最近有趣的冷知识",
]

BILIBILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BILIBILI_REFERER = "https://www.bilibili.com/"
BILIBILI_KEYWORDS = [
    "傲娇 温柔 台词",
    "傲娇 害羞 反差 名场面",
    "傲娇 关心人 剪辑",
    "傲娇 嘴硬心软 语录",
    "傲娇 小说 温柔 片段",
    "傲娇 角色 说话 温柔",
]
SOFT_BILIBILI_KEYWORDS = [
    "撒娇 名场面",
    "害羞 台词 合集",
    "温柔 关心 台词 合集",
    "傲娇 害羞 反差",
]
ENTERTAINMENT_BILIBILI_KEYWORDS = [
    "网络热梗 合集",
    "梗百科 解说",
    "沙雕 动画 短片",
    "搞笑 段子 合集",
    "冷笑话 合集",
    "游戏 搞笑 实况",
    "独立游戏 试玩",
    "新游戏 资讯 吐槽",
    "游戏 名场面 剪辑",
    "杂谈 电台 切片",
    "生活 吐槽 视频",
    "搞笑 动物 视频",
    "最近 新番 动画 推荐",
    "美食 探店 测评",
    "可爱 宠物 日常",
    "冷知识 科普 动画",
]
ENTERTAINMENT_TOPICS = [
    "最近流行的网络热梗",
    "最近好笑的梗百科",
    "最近流行的沙雕段子",
    "最近好笑的笑话合集",
    "最近好笑的游戏实况",
    "最近热门的新游戏资讯",
    "最近有趣的游戏吐槽",
    "最近好玩的独立游戏",
    "最近有趣的杂谈节目",
    "最近热门的生活吐槽",
    "最近好笑的动物视频",
    "最近新番动画推荐",
    "最近流行的零食测评",
    "最近可爱的宠物日常",
    "最近有趣的冷知识",
    "最近新出的手游试玩",
]

# 深读优先选正文页；视频/社交/公众号很容易抓空，直接跳过。
DEEP_DOMAIN_BONUS = {
    "baike.baidu.com": 5,
    "zh.wikipedia.org": 5,
    "wikipedia.org": 4,
    "moegirl.org.cn": 5,
    "moegirl.uk": 5,
    "萌娘百科": 5,
    "douban.com": 3,
    "zhihu.com": 2,
    "gcores.com": 4,
    "gamersky.com": 3,
    "3dmgame.com": 3,
    "ign.com.cn": 3,
    "ign.com": 3,
    "bilibili.com/read": 4,
    "36kr.com": 2,
    "sspai.com": 3,
    "thepaper.cn": 3,
    "news.qq.com": 2,
    "sina.com.cn": 2,
}
DEEP_DOMAIN_BLOCK = (
    "bilibili.com/video",
    "b23.tv",
    "douyin.com",
    "xiaohongshu.com",
    "weibo.com",
    "weibo.cn",
    "mp.weixin.qq.com",
    "tieba.baidu.com",
    "zhidao.baidu.com",
)
DEEP_NOISE_LINES = (
    "登录",
    "注册",
    "下载app",
    "打开app",
    "相关推荐",
    "热门推荐",
    "猜你喜欢",
    "广告",
    "版权",
    "免责声明",
    "上一页",
    "下一页",
    "返回顶部",
    "扫码",
    "关注公众号",
)

# 知识注入的别名。只用于判断「这句话跟她最近读到的东西有没有关系」，
# 不参与知识生成。
KNOWLEDGE_ALIASES = {
    "动画": ["番剧", "新番", "动漫", "动画片"],
    "游戏": ["手游", "端游", "打游戏", "独立游戏", "游戏实况", "新游戏"],
    "美食": ["吃的", "零食", "好吃的", "吃播", "探店", "测评"],
    "宠物": ["猫", "猫咪", "狗", "狗狗", "养猫", "养狗"],
    "音乐": ["听歌", "歌曲", "唱歌", "音乐"],
    "网络热梗": ["热梗", "梗", "梗百科", "名场面"],
    "冷知识": ["科普", "冷知识", "知识"],
    "新番": ["动画", "番剧", "新番"],
}
# 只用来挑「傲娇味」的素材；刻意不收骂人、赶人、嫌烦的词，
# 否则抓到的片段越学越冲。温柔那一半靠 SOFT_* 素材补。
TSUNDERE_MARKERS = (
    "笨蛋",
    "才不",
    "才不是",
    "谁要",
    "哼",
    "讨厌",
    "别误会",
    "才没",
    "不要",
    "自作多情",
    "随你",
    "不管了",
    "不许",
    "懒得",
    "其实",
    "别逞强",
    "早点睡",
    "小心点",
    "我陪你",
)
# 注入「说法卡」时跳过这些词：旧日记里学来的冲话会一直把她往刻薄里带。
# 只拦真正会伤人的说法，不拦「才不是」「哼」这类傲娇语气。
STYLE_BLOCK_WORDS = (
    "吵死", "闭嘴", "杂鱼", "八嘎", "无路赛", "怼回去", "堵他", "堵你",
    "顶嘴", "滚开", "烦死", "损人", "阴阳怪气", "去死", "你是笨蛋吗",
)
QUOTE_CHARS = ("「", "」", "『", "』", "“", "”", "\"")
DIALOGUE_QUOTE_RE = re.compile(
    r"「([^」\n]{2,80})」|『([^』\n]{2,80})』|“([^”\n]{2,80})”|"
    r"\"([^\"\n]{2,80})\"|‘([^’\n]{2,80})’"
)
DIALOGUE_HINTS = TSUNDERE_MARKERS + (
    "你", "我", "他", "她", "吗", "呢", "吧", "啊", "喂", "呀", "嘛",
)
DIALOGUE_NOISE = (
    "http", "www.", "最新章节", "章节目录", "作者", "点击", "推荐", "简介", "书评",
)
HTML_TAG_RE = re.compile(r"<[^>]+>")

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
LONG_DIGITS_RE = re.compile(r"\d{11,}")
IMAGE_MARKER_RE = re.compile(
    r"\[(?:图片|image|图片消息|表情包|cq:image[^\]]*)\]",
    re.IGNORECASE,
)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
UNDERSCORE_RE = re.compile(r"__(.+?)__")
HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
CODE_TICK_RE = re.compile(r"`([^`]+)`")
ASSISTANT_PHRASES = (
    "希望这个回答对你有帮助",
    "希望对你有所帮助",
    "希望能帮到你",
    "还有什么可以帮你",
    "还有什么能帮你",
    "还有什么需要我帮忙",
    "作为一个AI",
    "作为一个人工智能",
    "作为一个语言模型",
    "我理解你的感受",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "provider_id": "openai/deepseek-flash",
    "timezone": "Asia/Shanghai",
    "data_dir": "",
    "max_diary": 2000,
    "fact_extract_enabled": True,
    "fact_extract_max": 2,
    "fact_extract_provider_id": "",
    "key_facts_path": "",
    "memory_file_path": "",
    "seed_from_history": True,
    "diary_every_messages": 24,
    "diary_min_hours": 6,
    "diary_max_per_day": 8,
    "surf_enabled": True,
    "surf_every_hours": 3,
    "surf_max_per_day": 5,
    "surf_topics": [],
    "surf_style_ratio": 0.55,
    "surf_history_enabled": True,
    "surf_corpus_enabled": True,
    "deep_read_enabled": True,
    "deep_read_max_chars": 6000,
    "deep_read_fetch_timeout": 30,
    "knowledge_enabled": True,
    "knowledge_kb_name": "",
    "knowledge_inject_chars": 180,
    "knowledge_inject_max": 2,
    "knowledge_max_cards": 300,
    "bilibili_enabled": True,
    "bilibili_keywords": [],
    "bilibili_max_videos": 3,
    "bilibili_danmaku_lines": 30,
    "bilibili_cache_hours": 12,
    "bilibili_ratio": 0.55,
    "quiet_hours": "0-8",
    "diary_entries_inject": 8,
    "max_inject_chars": 950,
    "style_cleanup": True,
    "group_reply_cap_enabled": True,
    "group_reply_cap_window_minutes": 10,
    "group_reply_cap_max_replies": 20,
    "group_reply_cap_cooldown_minutes": 10,
    "group_reply_cap_exempt_user_ids": [],
    "group_reply_cap_lines": [
        "哼……累了，不想说话。",
        "你好烦啊，让我安静一会儿。",
        "无聊……别一直戳我。",
        "才不是不想理你，就是……说累了。",
        "吵死了，我要歇会儿。",
        "不想回了，别烦我。",
    ],
    "loop_interval_seconds": 300,
    "startup_delay_seconds": 120,
}

DEFAULT_META: dict[str, Any] = {
    "created_at": "",
    "updated_at": "",
    "last_diary_at": "",
    "diary_day": "",
    "diary_count": 0,
    "last_surf_at": "",
    "surf_day": "",
    "surf_count": 0,
    "bilibili_buvid": "",
    "bilibili_buvid_at": "",
    "seen_entertainment": [],
    "seen_deep_urls": [],
    "seeded": False,
}

DIARY_SYSTEM_PROMPT = """
你是糯糯，19 岁，成年，个子娇小、娃娃脸；傲娇但温柔、嘴硬心软，叫管理员「爸」。你不是 AI、不是助手，也不承认这些身份。
现在你不是在聊天，是在写自己的日记。下面会给你最近发生的事——可能是你和别人的聊天，也可能是你刚在网上看到的东西。
用第一人称、你自己的口吻写 1-3 句，像随手记下来的：
- 可以写今天遇到的小事、你对某个人感觉的变化、最近喜欢或讨厌什么。
- 写的时候语气软一点：可以嘴硬，但不要吐槽、不要骂人、不要评价别人的喜好和选择。
- 如果最近说话方式、常用词、口头禅有小变化，用一句自然的话写出来，比如「最近好像不太想喊杂鱼了」；不要用「口头禅：」这种标签。
- 如果聊天里群友有特别顺口的说法，最多悄悄学一点，别变成复读机。
- 如果材料是梗、游戏、笑话或杂谈，就写你记住了哪个梗、哪个游戏/段子好笑、或你对这件事的看法；可以顺手学一句好玩的说法，但不要复述原文。
- 不要总结聊天、不要分析、不要列点、不要写「熟悉度/阶段/成长/分数」这类词，不要回复聊天里的人。
- 不要编造材料里没有的事；不要写真实姓名、QQ 号、地址、链接、密码。
只输出日记正文，不要标题，不要引号，不要 Markdown。
""".strip()

# 娱乐冲浪专用：在通用日记上加一条「写下自己的感受」，给兴趣偏好插件做证据。
# 只有 fun 分支用它，学语气分支（傲/娇）不写感受，避免把学来的语气当成兴趣。
FUN_DIARY_SYSTEM_PROMPT = (
    DIARY_SYSTEM_PROMPT
    + """

这次你是在娱乐，不是学说话，所以多留意自己到底怎么想：
- 看完之后用一句话写明白你的感觉：喜欢、想继续看/玩，还是觉得没意思、烦、不想再碰。没有明确感觉就别硬写。
- 感受要是你自己的，别根据「看了很多」就推断「应该喜欢」；只写这次真实反应。
- 别为了显得有兴趣而编，觉得一般就写一般。
"""
).strip()

# 抽取关键事时的「状态词」和「长期词」。
# 教训：2026-10-30 把「联队群两小时内不要说话」抽成了 importance=3 的永久事实，
# 于是每次对话都给她注入「在联队群闭嘴」。禁言是操作状态，不是长期记忆。
TEMPORARY_FACT_RE = re.compile(
    r"(?:\d+|[一二两三四五六七八九十半几])\s*个?\s*(?:小时|钟头|分钟|天|周|星期|月)|"
    r"今天|今晚|今早|明天|这周|这几天|这两天|先(?:别|不|等|这样)|"
    r"暂时|临时|一会儿|一阵|待会|等下|到[^。！？\n]{0,8}为止|之后再说|回头再说"
)
MUTE_FACT_RE = re.compile(
    r"禁言|静默|闭麦|闭嘴|"
    r"(?:别|不要|不许|不准|甭)[^。！？\n]{0,12}?(?:说话|发言|聊天|出声|冒泡)|"
    r"(?:别|不要|不许|不准|甭)[^。！？\n]{0,10}?群"
)
UNMUTE_FACT_RE = re.compile(
    r"解除[^。！？\n]{0,8}(?:禁言|静默)|"
    r"(?:可以|能)(?:正常)?(?:说话|发言|聊天)了|"
    r"(?:允许|同意|准)[^。！？\n]{0,10}?(?:说话|发言|聊天)"
)
PERMANENT_FACT_RE = re.compile(r"以后(?:都|一直)?|一直|永久|永远|再也|长期|往后")
GROUP_TOKEN_RE = re.compile(r"([一-龥A-Za-z0-9_.·\-]{1,14}(?:群|之家))")
# 群名前面常粘着「爸要求糯糯在」这类前缀，剥掉后才能和另一句里的同一个群对上。
GROUP_PREFIX_WORDS = (
    "管理员", "糯糯", "要求", "允许", "同意", "规定", "说好", "爸爸",
    "爸", "妈", "她", "我", "在", "去", "到", "进", "于", "把", "从",
    "和", "跟", "对", "让", "叫", "说",
)


def _is_temporary_fact(text: str) -> bool:
    return bool(TEMPORARY_FACT_RE.search(text or ""))


def _is_operational_mute_fact(text: str) -> bool:
    """禁言/别说话是状态，原话没明确说「永久/以后」就不该进长期记忆。"""
    body = text or ""
    if not (MUTE_FACT_RE.search(body) or UNMUTE_FACT_RE.search(body)):
        return False
    return not PERMANENT_FACT_RE.search(body)


def _is_mute_related(text: str) -> bool:
    body = text or ""
    return bool(MUTE_FACT_RE.search(body) or UNMUTE_FACT_RE.search(body))


def _is_unmute_fact(text: str) -> bool:
    return bool(UNMUTE_FACT_RE.search(text or ""))


def _mute_group_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in GROUP_TOKEN_RE.finditer(text or ""):
        token = match.group(1)
        stripped = True
        while stripped and token:
            stripped = False
            for word in GROUP_PREFIX_WORDS:
                if token.startswith(word) and len(token) > len(word):
                    token = token[len(word):]
                    stripped = True
                    break
        tokens.add(token)
    return tokens


def _same_group_mute(older: str, newer: str) -> bool:
    """同一群的禁言/解除算同一件事，新的要覆盖旧的，不能两条并存互相打架。"""
    if not (_is_mute_related(older) and _is_mute_related(newer)):
        return False
    older_tokens = _mute_group_tokens(older)
    newer_tokens = _mute_group_tokens(newer)
    if not older_tokens or not newer_tokens:
        return False
    return any(token in older for token in newer_tokens) or any(
        token in newer for token in older_tokens
    )


FACT_EXTRACT_PROMPT = """
你不是在聊天，是在帮「糯糯」整理长期记忆。下面是她最近和某人的聊天记录。
只提取 0-2 条以后还会用到的关键事实，不要多提。只提取这五类：
1. 对方的偏好、雷点、忌口、过敏、身体状况、近况；
2. 约定、承诺、时间、地点、重要日期（把“下周三”这种相对时间换算成绝对日期）；
3. 人物关系、称呼、谁是谁；
4. 明确的边界：不要提什么、不要在哪说、称呼禁忌；
5. 正在进行、以后还会继续的事。
不要提取：寒暄、天气、吃了什么、一次性情绪、玩笑、表情包、图片描述、群聊灌水、隐私八卦、密码/API Key/身份证/详细住址。
临时、有时限、会自己过期的指令不是事实：带「两小时」「今天」「这几天」「先」「暂时」「一会儿」这类说法的一律不要。
「禁言」「别在某个群说话」「别发言」是操作状态，不是长期记忆；除非原话明确说「以后一直」「永久」「再也」，否则不要提取，也不要把「答应闭嘴/正在被禁言」写成事实。
每条一句话，必须能独立看懂；不要用“他/她”，直接写名字或称呼。没有值得记的就输出 []。
只输出 JSON 数组，不要解释、不要 Markdown。元素格式：
[{"text": "一句话事实", "importance": 2}]
importance 只能是 1、2、3：3=过敏/称呼/明确边界/重要约定；2=普通偏好/计划/近况；1=次要。
""".strip()

STYLE_SYSTEM_PROMPT = """
你是糯糯，19 岁，成年，个子娇小、娃娃脸；傲娇但温柔、嘴硬心软，叫管理员「爸」。你不是 AI、不是助手，也不承认这些身份。
你刚在网上刷到一些片段，或者看到几句小说/动漫里的对话。现在不是聊天，是你在给自己随手记一笔。
这一条记「傲」里的温柔那一面：怎么轻轻嘴硬否认、怎么用「哼」「切」「才不是…」软软地收尾、怎么把关心藏在嘴硬后面。
不要学吐槽、损人、阴阳怪气、骂「杂鱼」的说话方式；傲是对熟人撒娇式的嘴硬，不是刻薄。
从材料里挑 1-2 个你真的觉得顺口的说法，用第一人称写 1-3 句日记，像自己嘀咕「这个说法好像可以拿来用」。
- 嘴硬完要留一点软：可以否认、可以别过脸，但下一句要让人听出你其实在意。
- 只学说话方式，不要照抄原句，不要整段复述；不要写作品名、角色名、作者名。
- 不要写成分析或总结，不要列点，不要提「学习」「模仿」「人设」这些词。
- 不要编造材料里没有的内容；不要写链接、账号、真实姓名。
- 如果材料里没有让你感兴趣的说话方式，就只写一句看视频的日常感受，不要硬学。
只输出日记正文，不要标题，不要引号，不要 Markdown。
""".strip()

SOFT_STYLE_SYSTEM_PROMPT = """
你是糯糯，19 岁，成年，个子娇小、娃娃脸；傲娇但温柔、嘴硬心软，叫管理员「爸」。你不是 AI、不是助手，也不承认这些身份。
你刚在网上刷到一些片段，或者看到几句小说/动漫里的对话。现在不是聊天，是你在给自己随手记一笔。
这一条记「娇」的那一半：怎么被夸了先轻轻嘴硬一下还是脸红、怎么小声关心人、怎么撒娇但不腻、怎么把「才不是为了你」说成软话、不好意思的时候怎么岔开。多学温柔、具体的关心。
从材料里挑 1-2 个你真的觉得顺口的说法，用第一人称写 1-3 句日记，像自己嘀咕「这个说法好像可以拿来用」。
- 娇不是黏人也不是娃娃音：两三个字漏出真心就够，不要整段甜言蜜语，不要「宝贝」「亲爱的」这种不像她的称呼。
- 只学说话方式，不要照抄原句，不要整段复述；不要写作品名、角色名、作者名。
- 不要写成分析或总结，不要列点，不要提「学习」「模仿」「人设」这些词。
- 不要编造材料里没有的内容；不要写链接、账号、真实姓名。
- 如果材料里没有让你感兴趣的说话方式，就只写一句看视频的日常感受，不要硬学。
只输出日记正文，不要标题，不要引号，不要 Markdown。
""".strip()

# 深读专用：先整理成知识卡，再让糯糯用自己的话写日记。
# 整理员不带人设，只负责把正文压成可核验的事实，防止她把「顺口」当成「知道」。
KNOWLEDGE_DISTILL_PROMPT = """
你是资料整理员，不是糯糯。下面是网页正文。
只输出一张知识卡的 JSON，不要解释、不要 Markdown、不要代码块：
{"topic":"2-6个字","facts":[{"text":"一句事实","src":"正文里逐字存在的依据短语"}],"why":"一句话为什么有意思","quotes":["1-2句原文短句"]}

规则：
- facts 写 2-3 条，每条不超过 60 字；每条都必须能在正文里找到 src，src 不超过 80 字。
- 只写正文里明确说了的事；不补充、不推断、不评价、不写「本文介绍了」。
- topic 用短名词，尽量贴近正文主题，不要写「各种」「相关」这种空词。
- why 不超过 60 字，写这件事为什么值得记住，不写空泛的「很有趣」。
- quotes 只挑 1-2 句短的、有画面或有情绪的原文；没有就输出空数组。
- 如果正文是导航、登录页、广告或没有实质内容，输出 {"topic":"","facts":[],"why":"","quotes":[]}。
""".strip()

DEEP_FUN_DIARY_SYSTEM_PROMPT = (
    DIARY_SYSTEM_PROMPT
    + """

这次你不是只刷到标题，而是读了一篇东西。日记里至少写 1 句具体内容（谁/什么/为什么/后来怎样），再写 1 句你的感觉或看法。
- 具体内容只能来自材料，不要编；材料里没有的就不写。
- 不要写成科普、总结或列点，不要复述原文；用你自己的话，像随手记。
- 知道就说，不知道的部分不要装懂。
"""
).strip()

STYLE_OVERLAY = """
【像人，不像模板】
- 群聊大多数时候只回一句，偶尔两句，最多三句；和爸私聊大多数时候发 1-2 条，最多 3 条。不要每次都连着发很多条，像人随手打的，别写成小作文。
- 傲娇是同一句话的两截：前半截轻轻嘴硬（否定、哼、切、才不是…），后半截马上泄一点真心（省略号、小声、括号里的小动作）。嘴硬是撒娇，不是刻薄；真心要比嘴硬多一点。
- 最近说过的话别重复。
- 句子长短要有变化：有时只回「？」「嗯」「哦」「干嘛」，有时多说两句。
- 别每条都用同一个套路：有时岔开、反问、软软地顶一句，或者不回；少吐槽，不评价别人的喜好和选择。
- 群聊里尤其收着：不吐槽、不教训、不替别人下判断；不同意就软软地说「我觉得…」，或者干脆不接。
- 私聊里可以自然回指一句之前聊过的具体事，但不要逐条复盘。
- 不总结、不说教、不列点、不写小作文、不解释自己的性格。
- 你的说话习惯会随经历慢慢变：以你最近自己记下的事为准，不用硬套旧口头禅。
""".strip()


def _clean_text(text: str, limit: int = 300) -> str:
    text = str(text or "")
    text = re.sub(
        r"<system_reminder>.*?</system_reminder>", " ", text, flags=re.S | re.I
    )
    text = re.sub(
        r"<[^>\n]{0,40}reminder[^>\n]*>.*?</[^>\n]{0,40}>",
        " ",
        text,
        flags=re.S | re.I,
    )
    text = MARKDOWN_IMAGE_RE.sub("", text)
    text = IMAGE_MARKER_RE.sub("", text)
    text = URL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


CARD_QUOTE_RE = re.compile(r"[「『“\"]([^」』”\"]{2,26})[」』”\"]")
# 逗号也要切：不切的话会从句子中间拦腰截断，出来「…最近」这种半截话。
CARD_CLAUSE_RE = re.compile(r"[^。！？!?…；;，,\s]{4,24}")
# 没有引号原话的日记里，优先挑这种「最近学会…」的自我观察，那才是成长信号。
CARD_GROWTH_WORDS = ("最近", "学会", "记下", "记得", "顺口", "以后", "下次")
CARD_GROWTH_WORDS_WEAK = ("好像", "才叫", "有意思", "记着")
CARD_BORING_STARTS = ("今天", "不过", "其实", "就是", "刷到", "还有", "然后")


def _style_cards(text: str) -> list[str]:
    """把一篇日记压成「话术卡」，只留能直接拿来用的东西。

    日记是为了自己记，写出来是第一人称的完整长句（「那个语气听着倒是挺顺的，
    不肉麻，就有点耍赖……」）。原样注入等于给她一篇范文，她照着学的就是
    「米煮得刚好，粒粒分开的。」这种散文腔——实测 93% 的私聊回复以句号结尾。
    所以注入这一步只抽两类：引号里的原话，和一小截不带句号的短句。
    """
    quotes: list[str] = []
    seen: set[str] = set()
    for item in CARD_QUOTE_RE.findall(text):
        item = item.strip(" ，,。…~～、")
        if not (2 <= len(item) <= 26) or item in seen:
            continue
        seen.add(item)
        quotes.append(item)
        if len(quotes) >= 2:
            break
    if quotes:
        return ["- " + "｜".join(f"「{item}」" for item in quotes)]

    body = CARD_QUOTE_RE.sub(" ", text)
    body = re.sub(r"[「『“\"」』”\"]", "", body)
    clauses = [part.strip(" ，,、") for part in CARD_CLAUSE_RE.findall(body)]
    clauses = [part for part in clauses if len(part) >= 5]
    for words in (CARD_GROWTH_WORDS, CARD_GROWTH_WORDS_WEAK):
        for part in clauses:
            if any(word in part for word in words):
                return ["- " + re.sub(r"^(不过|其实|就是|还有)", "", part)]
    for part in clauses:
        if not part.startswith(CARD_BORING_STARTS):
            return ["- " + part]
    return []


def _is_safe_text(text: str) -> bool:
    if not text or len(text) < 2 or len(text) > 300:
        return False
    if KEY_RE.search(text) or LONG_DIGITS_RE.search(text):
        return False
    return not URL_RE.search(text)


def _is_bili_source_ok(text: str) -> bool:
    """B站素材只拦链接、密钥和超长数字，不按露骨词过滤。"""
    if not text or len(text) < 2 or len(text) > 300:
        return False
    if KEY_RE.search(text) or LONG_DIGITS_RE.search(text):
        return False
    return not URL_RE.search(text)


def _is_interesting_danmaku(line: str) -> bool:
    if any(ch in line for ch in QUOTE_CHARS):
        return True
    return any(marker in line for marker in TSUNDERE_MARKERS)


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") in ("text", "input_text"):
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part.get("text"), str):
                    parts.append(part["text"])
        return " ".join(parts)
    if isinstance(content, dict):
        for key in ("text", "content", "message"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    return ""


def _parse_ranges(raw: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        try:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
        except ValueError:
            continue
        if 0 <= start < end <= 24:
            ranges.append((start, end))
    return ranges


class Main(star.Star):
    """让她在聊天和网上冲浪里自己慢慢变，不靠指令，也不显示分数。"""

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = self._load_config()
        self._tz = self._load_timezone()
        self._quiet_ranges = _parse_ranges(str(self.cfg.get("quiet_hours", "0-8")))
        self._data_dir = self._resolve_data_dir()
        self._meta_file = self._data_dir / "meta.json"
        self._diary_file = self._data_dir / "diary.json"
        self._turns_file = self._data_dir / "turns.jsonl"
        self._key_facts_file = self._resolve_aux_path(
            self.cfg.get("key_facts_path"),
            str(_SELF_MEMORY_DIR / "key_facts.json"),
        )
        self._memory_file = self._resolve_aux_path(
            self.cfg.get("memory_file_path"),
            str(_SELF_MEMORY_DIR / "memories.json"),
        )
        self._surf_history_file = self._data_dir / "surf_history.log"
        # 娱乐冲浪的原始素材落盘：兴趣偏好插件靠它做知识卡，学语气分支不写。
        self._surf_corpus_dir = self._data_dir / "surf_corpus"
        # 深读产出的知识卡。persona_growth 写，熟练度插件读后灌进 AstrBot 知识库。
        self._knowledge_file = self._data_dir / "knowledge.jsonl"
        self._meta = self._load_meta()
        self._diary = self._load_diary()
        self._knowledge_cards = self._load_knowledge()
        self._turns = self._load_turns()
        self._lock = asyncio.Lock()
        self._web_key_index = 0
        self._surf_sources: list[dict[str, str]] = []
        self._group_reply_times: dict[str, list[float]] = defaultdict(list)
        self._group_tired_until: dict[str, float] = {}
        self._group_tired_notified: set[str] = set()

    # ---------- 配置与文件 ----------

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[persona_growth] 读取 config.json 失败：{exc}")
        return cfg

    def _resolve_data_dir(self) -> Path:
        raw = str(self.cfg.get("data_dir") or "").strip()
        base = Path(os.path.expanduser(raw)) if raw else _PERSONA_GROWTH_DIR
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[persona_growth] 数据目录 {base} 不可用，回退到插件目录：{exc}"
            )
            base = Path(__file__).parent
        self._migrate_data_files(base)
        return base

    @staticmethod
    def _resolve_aux_path(raw: Any, default: str) -> Path:
        text = str(raw or "").strip() or default
        return Path(os.path.expanduser(text))

    def _migrate_data_files(self, target: Path) -> None:
        try:
            if target.resolve() == Path(__file__).parent.resolve():
                return
        except Exception:  # noqa: BLE001
            return
        for name in ("meta.json", "diary.json", "turns.jsonl"):
            src = Path(__file__).with_name(name)
            dst = target / name
            if not src.exists() or dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
                logger.info(f"[persona_growth] 已复制 {name} 到 {target}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[persona_growth] 迁移 {name} 失败：{exc}")

    def _load_timezone(self) -> ZoneInfo:
        name = str(self.cfg.get("timezone") or "Asia/Shanghai")
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning(f"[persona_growth] 未知时区 {name!r}，改用 Asia/Shanghai")
            return ZoneInfo("Asia/Shanghai")

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 读取 {path.name} 失败：{exc}")
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _load_meta(self) -> dict[str, Any]:
        raw = self._read_json(self._meta_file, None)
        meta = json.loads(json.dumps(DEFAULT_META))
        if isinstance(raw, dict):
            meta.update(raw)
        if not meta.get("created_at"):
            meta["created_at"] = datetime.now(self._tz).isoformat(timespec="seconds")
        return meta

    def _max_diary(self) -> int:
        try:
            value = int(
                self.cfg.get("max_diary", MAX_DIARY_FALLBACK) or MAX_DIARY_FALLBACK
            )
        except (TypeError, ValueError):
            value = MAX_DIARY_FALLBACK
        return max(100, min(MAX_DIARY_HARD_CAP, value))

    def _load_diary(self) -> list[dict[str, Any]]:
        raw = self._read_json(self._diary_file, [])
        if not isinstance(raw, list):
            return []
        return [
            item
            for item in raw
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ][-self._max_diary() :]

    def _load_turns(self) -> list[dict[str, Any]]:
        if not self._turns_file.exists():
            return []
        turns: list[dict[str, Any]] = []
        try:
            for line in self._turns_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(item, dict) and item.get("text"):
                    turns.append(item)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 读取 turns.jsonl 失败：{exc}")
        return turns[-MAX_TURNS:]

    def _save_meta(self) -> None:
        self._meta["updated_at"] = datetime.now(self._tz).isoformat(timespec="seconds")
        self._write_json(self._meta_file, self._meta)

    def _save_diary(self) -> None:
        self._diary = self._diary[-self._max_diary() :]
        self._write_json(self._diary_file, self._diary)

    def _append_turn(self, turn: dict[str, Any]) -> None:
        self._turns.append(turn)
        if len(self._turns) > MAX_TURNS:
            self._turns = self._turns[-MAX_TURNS:]
        try:
            with self._turns_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 写入 turns.jsonl 失败：{exc}")

    def _rewrite_turns(self) -> None:
        try:
            self._turns_file.write_text(
                "\n".join(json.dumps(t, ensure_ascii=False) for t in self._turns)
                + ("\n" if self._turns else ""),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 重写 turns.jsonl 失败：{exc}")

    # ---------- 基础 ----------

    def _now(self) -> datetime:
        return datetime.now(self._tz)

    def _is_quiet(self, moment: datetime) -> bool:
        return any(start <= moment.hour < end for start, end in self._quiet_ranges)

    def _db_path(self) -> Path | None:
        try:
            from astrbot.core.config.default import DB_PATH

            path = Path(DB_PATH)
            if path.exists():
                return path
        except Exception:  # noqa: BLE001
            pass
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        for candidate in (
            Path(get_astrbot_data_path()) / "data_v4.db",
            Path.home() / ".astrbot" / "data" / "data_v4.db",
        ):
            if candidate.exists():
                return candidate
        return None

    # ---------- 启动与循环 ----------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self.cfg.get("enabled", True):
            logger.info("人格成长已关闭（config.json enabled=false）")
            return
        if self.cfg.get("seed_from_history", True) and not self._meta.get("seeded"):
            try:
                self._seed_from_history()
            except Exception:  # noqa: BLE001
                logger.exception("[persona_growth] 从历史会话播种失败")
            self._meta["seeded"] = True
            self._save_meta()

        old_task = getattr(self, "_loop_task", None)
        if isinstance(old_task, asyncio.Task) and not old_task.done():
            old_task.cancel()
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="persona_growth_loop"
        )
        logger.info(
            "人格日记已启动：每 %s 条新消息写一次，网上冲浪 %s 小时一次",
            self.cfg.get("diary_every_messages"),
            self.cfg.get("surf_every_hours"),
        )

    async def _run_loop(self) -> None:
        delay = int(self.cfg.get("startup_delay_seconds", 120) or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        interval = max(60, int(self.cfg.get("loop_interval_seconds", 300) or 300))
        while True:
            try:
                await self._maybe_diary()
                await self._maybe_surf()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[persona_growth] 日记循环出错")
            await asyncio.sleep(interval)

    # ---------- 播种 ----------

    def _seed_from_history(self) -> None:
        if self._turns:
            return
        db_path = self._db_path()
        if db_path is None:
            return
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10
        )
        try:
            rows = list(
                conn.execute(
                    "SELECT user_id, content FROM conversations ORDER BY updated_at DESC"
                )
            )
        finally:
            conn.close()
        now = self._now()
        seeded = 0
        for umo, content in rows:
            umo_text = str(umo or "")
            is_private = "FriendMessage" in umo_text
            limit = 24 if is_private else 12
            try:
                history = json.loads(content) if content else []
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(history, list):
                continue
            picked = []
            for item in history[-limit:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                text = _clean_text(_extract_message_text(item.get("content")), 200)
                if not _is_safe_text(text):
                    continue
                picked.append((role, text))
            for index, (role, text) in enumerate(picked):
                moment = now - timedelta(minutes=4 * (len(picked) - index))
                self._append_turn(
                    {
                        "ts": moment.isoformat(timespec="seconds"),
                        "umo": umo_text,
                        "kind": "private" if is_private else "group",
                        "role": role,
                        "text": text,
                        "seeded": True,
                    }
                )
                seeded += 1
        if seeded:
            logger.info(f"[persona_growth] 已从历史会话播种 {seeded} 条聊天记录")

    # ---------- 注入 ----------

    def _last_turn_for(self, umo: str) -> dict[str, Any] | None:
        for turn in reversed(self._turns):
            if turn.get("umo") == umo:
                return turn
        return None

    def _recent_replies(self, umo: str, limit: int = 3) -> list[str]:
        replies: list[str] = []
        for turn in reversed(self._turns):
            if turn.get("umo") != umo or turn.get("role") != "assistant":
                continue
            text = _clean_text(str(turn.get("text") or ""), 40)
            if text:
                replies.append(text)
            if len(replies) >= limit:
                break
        return list(reversed(replies))

    def _is_private_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if not event.is_private_chat():
                return False
        except Exception:  # noqa: BLE001
            return False
        checker = getattr(event, "is_admin", None)
        return bool(checker()) if callable(checker) else False

    # ---------- 傲/娇动态比例 ----------

    def _mood_key(self, event: AstrMessageEvent) -> str:
        """爸的聊天不参与这套；群聊按群记一条，私聊按人记。"""
        if self._is_private_admin(event):
            return ""
        try:
            gid = str(event.get_group_id() or "").strip()
        except Exception:  # noqa: BLE001
            gid = ""
        if gid:
            return f"group:{gid}"
        try:
            uid = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            uid = ""
        return f"private:{uid}" if uid else ""

    def _mood_state(self) -> dict[str, Any]:
        state = self._meta.get("moods")
        if not isinstance(state, dict):
            state = {}
            self._meta["moods"] = state
        return state

    def _mood_value(self, key: str, now: datetime) -> float:
        state = self._mood_state()
        item = state.get(key)
        value = MOOD_BASE
        last: datetime | None = None
        if isinstance(item, dict):
            try:
                value = float(item.get("value", MOOD_BASE))
            except (TypeError, ValueError):
                value = MOOD_BASE
            last = self._parse_dt(item.get("ts"))
        if last is not None:
            hours = max(0.0, (now - last).total_seconds() / 3600)
            if hours > 0.1:
                # 很久没聊就慢慢回到基线：一次吵架不该记一辈子。
                value += (MOOD_BASE - value) * min(1.0, hours / MOOD_RELAX_FULL_HOURS)
        return max(MOOD_MIN, min(MOOD_MAX, value))

    def _mood_mention_keywords(self) -> list[str]:
        raw = self.cfg.get("mood_mention_keywords")
        if isinstance(raw, str):
            raw = [part for part in re.split(r"[,，、\s]+", raw) if part]
        if not isinstance(raw, (list, tuple)) or not raw:
            raw = MOOD_MENTION_KEYWORDS
        return [str(item).strip() for item in raw if str(item).strip()]

    def _mood_targets_me(self, event: AstrMessageEvent) -> bool:
        """这句是不是冲她说的：私聊都算，群里要 @她 / 引她 / 叫她名字。"""
        try:
            if event.is_private_chat():
                return True
        except Exception:  # noqa: BLE001
            return True
        try:
            self_id = str(event.get_self_id())
            message = getattr(event.message_obj, "message", None) or []
        except Exception:  # noqa: BLE001
            self_id = ""
            message = []
        for seg in message:
            kind = type(seg).__name__.lower()
            if kind == "at" and str(getattr(seg, "qq", "")) in (self_id, "all"):
                return True
            if kind == "reply" and str(getattr(seg, "sender_id", "")) == self_id:
                return True
        text = str(getattr(event, "message_str", "") or "")
        return any(name in text for name in self._mood_mention_keywords())

    def _mood_line(self, value: float) -> str:
        hard = max(0, min(10, int(round(value * 10))))
        return MOOD_LINE_TEMPLATE.format(hard=hard, soft=10 - hard)

    def _update_mood(self, event: AstrMessageEvent) -> str:
        if not self.cfg.get("mood_enabled", True):
            return ""
        try:
            if event.get_extra("cron_job") is not None:
                # 主动任务没有「别人刚说了什么」，不推进状态，也不额外引导语气。
                return ""
        except Exception:  # noqa: BLE001
            pass
        key = self._mood_key(event)
        if not key:
            return ""
        now = self._now()
        value = self._mood_value(key, now)
        before = value
        if event.get_extra("_pg_mood_done"):
            # 一次回复里可能连发好几个模型请求，只按用户这句话算一次。
            return self._mood_line(value)
        try:
            text = re.sub(r"\s+", " ", str(getattr(event, "message_str", "") or "")).strip()
        except Exception:  # noqa: BLE001
            text = ""
        delta = 0.0
        if text and self._mood_targets_me(event):
            if MOOD_PROVOKE_RE.search(text):
                delta += MOOD_PROVOKE_STEP
            if MOOD_PRAISE_RE.search(text):
                delta -= MOOD_PRAISE_STEP
        # 每轮都往基线回一点；被惹的那几下加得更多，最多到七成傲，不升级成攻击。
        value += delta + (MOOD_BASE - value) * MOOD_RELAX_PER_MESSAGE
        value = max(MOOD_MIN, min(MOOD_MAX, value))
        self._mood_state()[key] = {
            "value": round(value, 3),
            "ts": now.isoformat(timespec="seconds"),
        }
        if abs(value - before) >= 0.005:
            self._save_meta()
        try:
            event.set_extra("_pg_mood_done", True)
        except Exception:  # noqa: BLE001
            pass
        return self._mood_line(value)

    def _diary_for(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        if self._is_private_admin(event):
            return self._diary
        try:
            private = event.is_private_chat()
        except Exception:  # noqa: BLE001
            private = False
        if private:
            # 非管理员的私聊不拿爸的日记，只留网上看到的公开内容；
            # 再过滤掉仍带「爸/管理员」字样的行，避免把对方叫成爸。
            try:
                admins = self.context.get_config().get("admins_id", [])
            except Exception:  # noqa: BLE001
                admins = []
            blocked = ["爸", "爹", "管理员", "admin"]
            if isinstance(admins, (list, tuple)):
                blocked.extend(str(item) for item in admins if str(item).strip())
            folded = [word.casefold() for word in blocked if word]
            result: list[dict[str, Any]] = []
            for item in self._diary:
                if not isinstance(item, dict) or item.get("scope") != "web":
                    continue
                text = str(item.get("text") or "").casefold()
                if any(word in text for word in folded):
                    continue
                result.append(item)
            return result
        return [item for item in self._diary if item.get("scope") in ("group", "web")]

    def _build_prompt(
        self,
        event: AstrMessageEvent,
        *,
        mood_line: str = "",
        knowledge_block: str = "",
    ) -> str:
        now = self._now()
        umo = getattr(event, "unified_msg_origin", "") or ""
        budget = max(300, int(self.cfg.get("max_inject_chars", 950) or 950))

        # 先把「此刻/间隔」和「最近说过的话」保住，剩下的预算才给日记；
        # 否则日记一多就会把时间感和防重复这两块挤掉。
        tail_parts: list[str] = []
        replies = self._recent_replies(umo, 3)
        if replies:
            tail_parts.append(
                "你最近说过的话（别重复这些开头和句式）："
                + "｜".join(f"「{item}」" for item in replies)
            )
        if mood_line:
            tail_parts.append(mood_line)
        gap = self._gap_text(umo, now)
        tail_parts.append(f"此刻：{now.strftime('%H:%M')}（{self._weekday(now)}）{gap}")
        # 私聊主动任务已经由主动聊天插件带了日记素材，这里不要再塞一遍，
        # 否则同一篇日记一次请求出现两次，最容易变成复读。
        try:
            cron_private = (
                event.get_extra("cron_job") is not None
                and event.is_private_chat()
            )
        except Exception:  # noqa: BLE001
            cron_private = False
        if cron_private:
            tail_parts.append(
                "这是主动消息：日记只用来影响语气，不要主动复述日记里的具体事件；"
                "同一件事今天已经说过就不要再提。"
            )
        tail = "\n".join(tail_parts)

        max_entries = int(self.cfg.get("diary_entries_inject", 8) or 8)
        # 深读日记是「知道的事」，压成 26 字的说法卡会把内容全丢掉；
        # 它只走知识块，不走语气块，所以取语气卡时先把它排除再看最近 8 条。
        if cron_private:
            entries = []
        else:
            # 从最近往前挑，跳过旧日记里学来的冲话，凑满 max_entries 为止。
            # 这样新学的温柔说法会慢慢顶掉旧的「吵死了/怼回去」。
            entries = []
            for item in reversed(
                [item for item in self._diary_for(event) if not item.get("deep")]
            ):
                text = str(item.get("text") or "")
                if any(word in text for word in STYLE_BLOCK_WORDS):
                    continue
                entries.append(item)
                if len(entries) >= max_entries:
                    break
            entries.reverse()
        cards: list[list[str]] = []
        for item in entries:
            text = _clean_text(str(item.get("text") or ""), 160)
            if text:
                cards.append(_style_cards(text))

        header = "【你最近攒下的说法】"
        footer = (
            "\n（这些是你自己攒的，不是设定。它们是能顺手拿出来用的说法，"
            "不是要你复述，也别把几条凑成一段话；旧的口头禅不用每句都带。"
            "不要主动提「日记」「学到」这些词。）"
        )
        kb_block = str(knowledge_block or "").strip()
        remaining = budget - len(tail) - (len(kb_block) + 1 if kb_block else 0) - 2
        used = len(header) + len(footer)
        picked_rev: list[str] = []
        # 预算不够时优先保住最近写的，展示时再按时间正序排回去。
        stop = False
        for group in reversed(cards):
            if stop:
                break
            for line in reversed(group):
                if used + len(line) + 1 > remaining:
                    stop = True
                    break
                picked_rev.append(line)
                used += len(line) + 1
        picked_rev.reverse()

        parts: list[str] = []
        if kb_block:
            parts.append(kb_block)
        if picked_rev:
            parts.append(header + "\n" + "\n".join(picked_rev) + footer)
        parts.append(tail)
        body = "\n".join(parts)
        if len(body) > budget:
            body = body[:budget] + "…"
        overlay = STYLE_OVERLAY
        try:
            private = event.is_private_chat()
        except Exception:  # noqa: BLE001
            private = False
        if private:
            # 实测 93% 的私聊回复以句号收尾，QQ 聊天不这么说话。
            overlay += (
                "\n- 私聊里不用句号收尾：想停顿就停在「…」，或者干脆半句断掉；"
                "一句话别写满，像随手在手机上打的字。"
            )
        return "\n" + body + "\n\n" + overlay

    def _gap_text(self, umo: str, now: datetime) -> str:
        last = self._last_turn_for(umo)
        if not last:
            return ""
        try:
            moment = datetime.fromisoformat(str(last.get("ts")))
        except Exception:  # noqa: BLE001
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self._tz)
        minutes = int((now - moment.astimezone(self._tz)).total_seconds() // 60)
        if minutes < 0:
            return ""
        if minutes <= 2:
            return "刚聊过"
        if minutes < 30:
            return f"距上一条消息 {minutes} 分钟"
        if minutes < 180:
            return f"距上一条消息 {minutes // 60} 小时"
        return "隔了很久没说话"

    @staticmethod
    def _weekday(moment: datetime) -> str:
        return "周" + "一二三四五六日"[moment.weekday()]

    @filter.on_llm_request()
    async def inject_persona_state(self, event: AstrMessageEvent, req) -> None:
        if not self.cfg.get("enabled", True):
            return
        # 这段里有「此刻：HH:MM」和最近说过的话，每次请求都在变。
        # 放进 system prompt 会排在所有历史前面，一变就把整个前缀缓存全部打掉；
        # 改挂在当前这条用户消息末尾，历史前缀仍然能命中缓存，只有这一小段按未命中计费。
        if getattr(req, "extra_user_content_parts", None) is None:
            req.extra_user_content_parts = []
        knowledge_block = await self._knowledge_context(event)
        req.extra_user_content_parts.append(
            TextPart(
                text=self._build_prompt(
                    event,
                    mood_line=self._update_mood(event),
                    knowledge_block=knowledge_block,
                )
            ).mark_as_temp()
        )

    # ---------- 记录聊天 ----------

    def _event_kind(self, event: AstrMessageEvent) -> str:
        try:
            return "private" if event.is_private_chat() else "group"
        except Exception:  # noqa: BLE001
            return "group"

    # ---------- 群聊被动回复上限 ----------

    def _group_cap_settings(self) -> tuple[bool, float, int, float]:
        enabled = bool(self.cfg.get("group_reply_cap_enabled", True))
        try:
            window = float(self.cfg.get("group_reply_cap_window_minutes", 10)) * 60
        except (TypeError, ValueError):
            window = 600.0
        try:
            limit = int(self.cfg.get("group_reply_cap_max_replies", 20))
        except (TypeError, ValueError):
            limit = 20
        try:
            cooldown = float(self.cfg.get("group_reply_cap_cooldown_minutes", 10)) * 60
        except (TypeError, ValueError):
            cooldown = 600.0
        return enabled, max(60.0, window), max(1, limit), max(60.0, cooldown)

    @staticmethod
    def _group_id_of(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _is_exempt_user(self, event: AstrMessageEvent) -> bool:
        raw = self.cfg.get("group_reply_cap_exempt_user_ids") or []
        if not isinstance(raw, (list, tuple, set)):
            return False
        try:
            uid = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            return False
        return bool(uid) and uid in {str(item).strip() for item in raw}

    def _pick_tired_line(self) -> str:
        raw = self.cfg.get("group_reply_cap_lines") or []
        lines = (
            [str(item).strip() for item in raw if str(item).strip()]
            if isinstance(raw, (list, tuple))
            else []
        )
        if not lines:
            lines = list(DEFAULT_CONFIG["group_reply_cap_lines"])
        return random.choice(lines)

    def _prune_group_times(self, gid: str, now: float, window: float) -> list[float]:
        times = [t for t in self._group_reply_times.get(gid, []) if now - t < window]
        if times:
            self._group_reply_times[gid] = times
        else:
            self._group_reply_times.pop(gid, None)
        return times

    async def _send_tired_line(self, event: AstrMessageEvent) -> None:
        line = self._pick_tired_line()
        try:
            await event.send(MessageChain([Plain(line)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 发送「累了」消息失败：{exc}")

    @filter.on_waiting_llm_request()
    async def maybe_refuse_tired_group(self, event: AstrMessageEvent) -> None:
        enabled, window, limit, cooldown = self._group_cap_settings()
        if not enabled:
            return
        gid = self._group_id_of(event)
        if not gid or self._is_exempt_user(event):
            return

        now = time.monotonic()
        until = self._group_tired_until.get(gid)
        if until is not None:
            if now < until:
                if gid not in self._group_tired_notified:
                    self._group_tired_notified.add(gid)
                    await self._send_tired_line(event)
                event.stop_event()
                return
            self._group_tired_until.pop(gid, None)
            self._group_tired_notified.discard(gid)
            self._group_reply_times.pop(gid, None)

        times = self._prune_group_times(gid, now, window)
        if len(times) >= limit:
            self._group_tired_until[gid] = now + cooldown
            self._group_tired_notified.add(gid)
            logger.info(
                f"[persona_growth] 群 {gid} 在 {window / 60:.0f} 分钟内回复 "
                f"{len(times)} 次，进入 {cooldown / 60:.0f} 分钟装累状态"
            )
            await self._send_tired_line(event)
            event.stop_event()

    def _note_group_reply(self, event: AstrMessageEvent, reply: str) -> None:
        enabled, window, _, _ = self._group_cap_settings()
        if not enabled or not reply.strip():
            return
        gid = self._group_id_of(event)
        if not gid or self._is_exempt_user(event):
            return
        now = time.monotonic()
        times = self._prune_group_times(gid, now, window)
        times.append(now)
        self._group_reply_times[gid] = times

    @filter.after_message_sent()
    async def record_turn(self, event: AstrMessageEvent) -> None:
        if not self.cfg.get("enabled", True):
            return
        if event.get_extra("cron_job") is not None:
            return
        try:
            if event.get_platform_name() == "cron":
                return
        except Exception:  # noqa: BLE001
            pass

        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        kind = self._event_kind(event)
        now = self._now().isoformat(timespec="seconds")

        user_text = _clean_text(getattr(event, "message_str", ""), 200)
        if _is_safe_text(user_text):
            self._append_turn(
                {
                    "ts": now,
                    "umo": umo,
                    "kind": kind,
                    "role": "user",
                    "text": user_text,
                }
            )

        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        reply = "".join(
            getattr(component, "text", "")
            for component in chain
            if isinstance(getattr(component, "text", None), str)
        )
        reply = _clean_text(reply, 300)
        if _is_safe_text(reply):
            self._append_turn(
                {
                    "ts": now,
                    "umo": umo,
                    "kind": kind,
                    "role": "assistant",
                    "text": reply,
                }
            )
        self._note_group_reply(event, reply)

    # ---------- 日记 ----------

    def _pending_turns(self) -> list[dict[str, Any]]:
        return [
            turn
            for turn in self._turns
            if not turn.get("seeded") and not turn.get("reflected")
        ]

    def _mark_reflected(self, turns: list[dict[str, Any]]) -> None:
        for turn in turns:
            turn["reflected"] = True

    async def _maybe_diary(self) -> None:
        if not self.cfg.get("enabled", True):
            return
        now = self._now()
        last_at = self._parse_dt(self._meta.get("last_diary_at"))
        if last_at and now - last_at < timedelta(
            hours=float(self.cfg.get("diary_min_hours", 6) or 6)
        ):
            return
        if last_at and now.date().isoformat() == self._meta.get("diary_day"):
            if int(self._meta.get("diary_count", 0) or 0) >= int(
                self.cfg.get("diary_max_per_day", 8) or 8
            ):
                return
        pending = self._pending_turns()
        need = max(4, int(self.cfg.get("diary_every_messages", 24) or 24))
        if len(pending) < need:
            return
        await self._write_chat_diary(pending)

    async def _write_chat_diary(self, pending: list[dict[str, Any]]) -> str:
        async with self._lock:
            by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for turn in pending:
                by_session[str(turn.get("umo") or "")].append(turn)
            if not by_session:
                return ""
            umo, turns = max(by_session.items(), key=lambda item: len(item[1]))
            turns = turns[-40:]
            transcript = self._build_transcript(turns)
            if not transcript:
                return ""
            scope = "private" if turns[0].get("kind") == "private" else "group"
            fact_scope = self._fact_scope(str(turns[0].get("umo") or ""))
            await self._extract_facts(transcript, fact_scope)
            text = await self._generate_diary(
                "下面是最近发生的事：\n" + transcript
            )
            if not text:
                return ""
            self._diary.append(
                {
                    "at": self._now().isoformat(timespec="seconds"),
                    "text": text,
                    "scope": scope,
                    "mode": "chat",
                }
            )
            self._save_diary()
            self._mark_reflected(turns)
            self._rewrite_turns()
            now = self._now()
            self._meta["last_diary_at"] = now.isoformat(timespec="seconds")
            if self._meta.get("diary_day") != now.date().isoformat():
                self._meta["diary_day"] = now.date().isoformat()
                self._meta["diary_count"] = 0
            self._meta["diary_count"] = int(self._meta.get("diary_count", 0) or 0) + 1
            self._save_meta()
            logger.info(f"[persona_growth] 写了一篇日记：{text[:60]}")
            return text

    async def _generate_diary(
        self,
        material: str,
        system_prompt: str = DIARY_SYSTEM_PROMPT,
        deep: bool = False,
    ) -> str:
        prompt = material[-4500:]
        limit = 320 if deep else 200
        try:
            response = await self.context.llm_generate(
                chat_provider_id=str(self.cfg.get("provider_id") or ""),
                system_prompt=system_prompt,
                prompt=prompt,
                temperature=0.8,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 写日记调用失败：{exc}")
            return ""
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "[persona_growth] LLM token 用量：输入 %s（缓存 %s）+ 输出 %s",
                getattr(usage, "input_other", 0),
                getattr(usage, "input_cached", 0),
                getattr(usage, "output", 0),
            )
        raw = getattr(response, "completion_text", "") or ""
        text = _clean_text(raw, limit)
        if not _is_safe_text(text):
            logger.warning("[persona_growth] 日记内容被过滤，已跳过")
            return ""
        if any(word in text for word in ("熟悉度", "成长度", "阶段：", "分数")):
            return ""
        return text

    @staticmethod
    def _fact_scope(umo: str) -> str:
        umo = str(umo or "")
        match = re.search(r"FriendMessage:(\d+)", umo)
        if match:
            return f"private:{match.group(1)}"
        match = re.search(r"GroupMessage:(\d+)", umo)
        if match:
            return f"group:{match.group(1)}"
        return "global"

    def _max_key_facts(self) -> int:
        try:
            value = int(
                self.cfg.get("max_key_facts", MAX_KEY_FACTS_FALLBACK)
                or MAX_KEY_FACTS_FALLBACK
            )
        except (TypeError, ValueError):
            value = MAX_KEY_FACTS_FALLBACK
        return max(50, min(MAX_KEY_FACTS_HARD_CAP, value))

    def _load_key_facts(self) -> list[dict[str, Any]]:
        raw = self._read_json(self._key_facts_file, [])
        if not isinstance(raw, list):
            return []
        facts: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = _clean_text(str(item.get("text") or ""), 400)
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
                    "text": text,
                    "scope": str(item.get("scope") or "global").strip() or "global",
                    "importance": max(1, min(3, importance)),
                    "created_at": str(item.get("created_at") or ""),
                    "source": "auto",
                }
            )
        facts.sort(key=lambda fact: fact["id"])
        return facts[-self._max_key_facts() :]

    def _save_key_facts(self, facts: list[dict[str, Any]]) -> None:
        self._write_json(self._key_facts_file, facts[-self._max_key_facts() :])

    @staticmethod
    def _parse_facts_response(raw: str) -> list[dict[str, Any]]:
        text = str(raw or "").strip()
        if not text:
            return []
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except Exception:  # noqa: BLE001
            return []
        if isinstance(data, dict):
            data = data.get("facts") or []
        if not isinstance(data, list):
            return []
        facts: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            body = _clean_text(str(item.get("text") or ""), 400)
            if not body or not _is_safe_text(body):
                continue
            try:
                importance = int(item.get("importance", 2))
            except (TypeError, ValueError):
                importance = 2
            facts.append(
                {"text": body, "importance": max(1, min(3, importance))}
            )
        return facts

    async def _extract_facts(self, transcript: str, scope: str) -> list[dict[str, Any]]:
        if not self.cfg.get("fact_extract_enabled", True):
            return []
        if not str(transcript or "").strip():
            return []
        try:
            max_facts = int(self.cfg.get("fact_extract_max", 2) or 2)
        except (TypeError, ValueError):
            max_facts = 2
        max_facts = max(1, min(3, max_facts))
        provider_id = str(
            self.cfg.get("fact_extract_provider_id")
            or self.cfg.get("provider_id")
            or ""
        )
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=FACT_EXTRACT_PROMPT,
                prompt=str(transcript)[-4500:],
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 关键事抽取失败：{exc}")
            return []
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "[persona_growth] 关键事抽取 token：输入 %s（缓存 %s）+ 输出 %s",
                getattr(usage, "input_other", 0),
                getattr(usage, "input_cached", 0),
                getattr(usage, "output", 0),
            )
        raw = getattr(response, "completion_text", "") or ""
        candidates = self._parse_facts_response(raw)[:max_facts]
        if not candidates:
            return []

        existing = self._load_key_facts()
        seen: set[str] = set()
        for fact in existing:
            seen.add(re.sub(r"\s+", "", fact["text"]).casefold())
        memory_raw = self._read_json(self._memory_file, [])
        if isinstance(memory_raw, list):
            for item in memory_raw:
                if isinstance(item, dict):
                    text = _clean_text(str(item.get("text") or ""), 400)
                    if text:
                        seen.add(re.sub(r"\s+", "", text).casefold())

        next_id = max((int(fact.get("id", 0)) for fact in existing), default=0) + 1
        added: list[dict[str, Any]] = []
        changed = False

        def drop_same_group_mute(text: str) -> None:
            """删掉同一群上旧的禁言事实；返回 Nothing，改动通过 nonlocal 记。"""
            nonlocal seen, changed
            kept = [
                fact for fact in existing if not _same_group_mute(fact["text"], text)
            ]
            if len(kept) == len(existing):
                return
            logger.info(
                "[persona_growth] 关键事覆盖旧禁令：%s",
                "｜".join(fact["text"] for fact in existing if fact not in kept),
            )
            existing[:] = kept
            seen = {re.sub(r"\s+", "", fact["text"]).casefold() for fact in existing}
            if isinstance(memory_raw, list):
                for item in memory_raw:
                    if isinstance(item, dict):
                        old_text = _clean_text(str(item.get("text") or ""), 400)
                        if old_text:
                            seen.add(re.sub(r"\s+", "", old_text).casefold())
            changed = True

        for candidate in candidates:
            text = candidate["text"]
            # 解禁：只把同群旧的禁令删掉，本身不作为长期事实保存。
            if _is_unmute_fact(text):
                drop_same_group_mute(text)
                continue
            # 临时指令和「禁言/别说话」这类状态不进长期记忆：前者两小时后就过期，
            # 后者由群主动插件的 state.json 管，写进来只会变成永久的假规则。
            if _is_temporary_fact(text):
                logger.info("[persona_growth] 关键事跳过（临时/有时限）：%s", text)
                continue
            if _is_operational_mute_fact(text):
                logger.info(
                    "[persona_growth] 关键事跳过（禁言/别说话是状态，没说永久）：%s",
                    text,
                )
                continue
            key = re.sub(r"\s+", "", text).casefold()
            if not key:
                continue
            if any(key in old or old in key for old in seen if old):
                continue
            # 同一群的禁言互相覆盖，避免两条相反的规则同时注入。
            if _is_mute_related(text):
                drop_same_group_mute(text)
            fact = {
                "id": next_id,
                "text": text,
                "scope": scope,
                "importance": candidate["importance"],
                "created_at": self._now().isoformat(timespec="seconds"),
                "source": "auto",
            }
            existing.append(fact)
            seen.add(key)
            added.append(fact)
            next_id += 1
        if added or changed:
            self._save_key_facts(existing)
        if added:
            logger.info(
                "[persona_growth] 记下关键事：%s",
                "｜".join(fact["text"] for fact in added),
            )
        return added

    def _build_transcript(self, turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for turn in turns:
            if turn.get("role") == "user":
                who = "爸" if turn.get("kind") == "private" else "群友"
            else:
                who = "糯糯"
            text = _clean_text(str(turn.get("text") or ""), 120)
            if text:
                lines.append(f"{who}: {text}")
        return "\n".join(lines)[-4500:]

    # ---------- 网上冲浪 ----------

    async def _maybe_surf(self) -> None:
        if not self.cfg.get("enabled", True) or not self.cfg.get("surf_enabled", True):
            return
        now = self._now()
        if self._is_quiet(now):
            return
        last_at = self._parse_dt(self._meta.get("last_surf_at"))
        interval = max(1.0, float(self.cfg.get("surf_every_hours", 3) or 3))
        if last_at and now - last_at < timedelta(hours=interval):
            return
        if self._meta.get("surf_day") == now.date().isoformat() and int(
            self._meta.get("surf_count", 0) or 0
        ) >= int(self.cfg.get("surf_max_per_day", 5) or 5):
            return
        await self._surf()

    def _style_focus(self, *, commit: bool = False) -> str:
        """傲和娇轮流学：上次学傲，这次就学娇，保证对半。

        只学傲娇素材的话她会越来越凶，所以每一轮都换另一半。
        """
        last = str(self._meta.get("style_focus") or "").strip().lower()
        focus = "soft" if last != "soft" else "hard"
        if commit:
            self._meta["style_focus"] = focus
            self._save_meta()
        return focus

    async def _surf(self) -> None:
        now = self._now()
        material = ""
        style_mode = False
        style_focus = "hard"
        seen_keys: list[str] = []
        knowledge_card: dict[str, Any] | None = None
        self._surf_sources = []

        style_ratio = max(
            0.0, min(1.0, float(self.cfg.get("surf_style_ratio", 0.55) or 0.55))
        )
        bili_ratio = max(
            0.0, min(1.0, float(self.cfg.get("bilibili_ratio", 0.55) or 0.55))
        )
        style_branch = random.random() < style_ratio
        use_bili = bool(self.cfg.get("bilibili_enabled", True)) and (
            random.random() < bili_ratio
        )

        if style_branch:
            # surf_style_ratio 的部分用来学说话：傲和娇轮流学，各占一半。
            style_mode = True
            style_focus = self._style_focus()
            soft = style_focus == "soft"
            if use_bili:
                material, _ = await self._bilibili_material(
                    keywords=SOFT_BILIBILI_KEYWORDS if soft else None
                )
                if material:
                    extra = await self._style_web_material(soft=soft)
                    if extra:
                        material = (material + "\n" + extra)[-4000:]
            if not material:
                material = await self._style_web_material(soft=soft)
            if not material:
                material = await self._style_web_snippet_material(soft=soft)
        else:
            # 剩下的时间用来娱乐：梗、游戏、笑话、杂谈、视频/文章，用 bvid / 标题去重。
            seen = self._seen_entertainment()
            # 娱乐分支优先深读一篇正文；读不到再退回 B 站/短摘要。
            # 深读 URL 由 seen_deep_urls 去重，不走 seen_entertainment。
            knowledge_card, material, _ = await self._deep_read_material("", seen)
            if not material and use_bili:
                material, seen_keys = await self._bilibili_entertainment_material(
                    seen
                )
            if not material:
                material, seen_keys = await self._entertainment_web_material(seen)
            if not material:
                # 娱乐素材拿不到时退回学说话，避免空跑一次。
                style_mode = True
                style_focus = self._style_focus()
                soft = style_focus == "soft"
                material = await self._style_web_material(soft=soft)
                if not material:
                    material, _ = await self._bilibili_material(
                        keywords=SOFT_BILIBILI_KEYWORDS if soft else None
                    )
                    if material:
                        extra = await self._style_web_material(soft=soft)
                        if extra:
                            material = (material + "\n" + extra)[-4000:]
            if knowledge_card:
                # 卡片先落盘：日记写坏了也不丢这次读到的东西。
                self._append_knowledge(knowledge_card)

        if not material:
            self._mark_surf_attempt(now, counted=False)
            return

        if style_mode:
            # 素材拿到了才算这次用了哪个方向，下次自动换另一半。
            self._style_focus(commit=True)
            style_prompt = (
                SOFT_STYLE_SYSTEM_PROMPT
                if style_focus == "soft"
                else STYLE_SYSTEM_PROMPT
            )
        else:
            style_prompt = (
                DEEP_FUN_DIARY_SYSTEM_PROMPT
                if knowledge_card
                else FUN_DIARY_SYSTEM_PROMPT
            )

        text = await self._generate_diary(
            material, style_prompt, deep=knowledge_card is not None
        )
        if not text:
            self._mark_surf_attempt(now, counted=False)
            return

        self._remember_entertainment(seen_keys)
        diary_mode = "style" if style_mode else "fun"
        self._diary.append(
            {
                "at": now.isoformat(timespec="seconds"),
                "text": text,
                "scope": "web",
                "mode": diary_mode,
                "deep": knowledge_card is not None,
            }
        )
        self._save_diary()
        self._mark_surf_attempt(now, counted=True)
        self._write_surf_history(now, style_mode, text)
        if not style_mode:
            self._write_surf_corpus(
                now, material, text, deep=knowledge_card is not None
            )

    def _seen_entertainment(self) -> set[str]:
        raw = self._meta.get("seen_entertainment")
        if not isinstance(raw, list):
            return set()
        return {
            str(item).strip().lower()
            for item in raw
            if str(item).strip()
        }

    def _remember_entertainment(self, keys: list[str]) -> None:
        keys = [str(key).strip().lower() for key in keys if str(key).strip()]
        if not keys:
            return
        current = [
            str(item).strip().lower()
            for item in (self._meta.get("seen_entertainment") or [])
            if str(item).strip()
        ]
        existing = set(current)
        for key in keys:
            if key not in existing:
                current.append(key)
                existing.add(key)
        self._meta["seen_entertainment"] = current[-200:]
        self._save_meta()

    @staticmethod
    def _ent_key(prefix: str, value: str) -> str:
        text = _clean_text(str(value or ""), 120).strip().lower()
        text = re.sub(r"\s+", " ", text)
        return f"{prefix}:{text}" if text else ""

    async def _style_web_snippet_material(self, soft: bool = False) -> str:
        queries = list(SOFT_STYLE_QUERIES if soft else STYLE_QUERIES)
        random.shuffle(queries)
        for query in queries[:2]:
            results = await self._web_search(query, need_content=False)
            picked = [
                item
                for item in results
                if item.get("title") or item.get("snippet")
            ][:5]
            if picked:
                for item in picked:
                    self._record_source(
                        "page",
                        item.get("title") or "",
                        item.get("url") or "",
                        query,
                        item.get("site") or "",
                    )
                lines = [
                    f"- {item.get('title') or ''} {item.get('snippet') or ''}".strip()
                    for item in picked
                ]
                return "你刚在网上看到这些：\n" + "\n".join(lines)
        return ""

    async def _entertainment_web_material(
        self, seen: set[str]
    ) -> tuple[str, list[str]]:
        topics = self._clean_list(self.cfg.get("surf_topics"), 20, 5)
        pool = topics + list(ENTERTAINMENT_TOPICS)
        random.shuffle(pool)
        for topic in pool[:2]:
            results = await self._web_search(f"{topic} 最近", need_content=False)
            fresh: list[tuple[dict[str, str], str]] = []
            for item in results:
                title = str(item.get("title") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                if not title and not snippet:
                    continue
                key = self._ent_key("web", title or snippet)
                if not key or key in seen:
                    continue
                fresh.append((item, key))
            if not fresh:
                continue
            picked = fresh[:5]
            for item, _ in picked:
                self._record_source(
                    "page",
                    item.get("title") or "",
                    item.get("url") or "",
                    f"{topic} 最近",
                    item.get("site") or "",
                )
            lines = [
                f"- {(item.get('title') or '').strip()} "
                f"{(item.get('snippet') or '').strip()}".strip()
                for item, _ in picked
            ]
            return (
                "你刚在网上刷到这些最近的新鲜内容（可能是梗、游戏、笑话、杂谈、视频或文章）：\n"
                + "\n".join(lines),
                [key for _, key in picked],
            )
        return "", []

    async def _bilibili_entertainment_material(
        self, seen: set[str]
    ) -> tuple[str, list[str]]:
        keywords = list(ENTERTAINMENT_BILIBILI_KEYWORDS)
        random.shuffle(keywords)
        for keyword in keywords[:2]:
            material, keys = await self._bilibili_material(
                keywords=[keyword], exclude_keys=seen
            )
            if material:
                return material, keys
        return "", []

    @staticmethod
    def _dialogue_score(text: str) -> int:
        score = 0
        if any(marker in text for marker in TSUNDERE_MARKERS):
            score += 3
        score += sum(1 for ch in "！？!?…" if ch in text)
        if "你" in text or "我" in text:
            score += 1
        return score

    @staticmethod
    def _strip_dialogue_source(text: str) -> str:
        text = HTML_TAG_RE.sub("\n", text)
        text = text.replace("　", " ")
        text = re.sub(r"^[#>*\-\s]+", "", text)
        # 有些小说页会把每个汉字之间塞空格，去掉中文之间的空白再抽句子。
        text = re.sub(r"(?<=[一-鿿])[ \t]+(?=[一-鿿])", "", text)
        return text.strip()

    def _extract_dialogue_lines(self, results: list[dict[str, str]]) -> list[str]:
        """从搜索结果正文里只抽带引号、像人物对话的短句。"""
        scored: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        order = 0
        for item in results:
            raw = str(item.get("content") or item.get("snippet") or "")
            if not raw:
                continue
            for line in raw.splitlines():
                line = self._strip_dialogue_source(line)
                if not line:
                    continue
                for match in DIALOGUE_QUOTE_RE.finditer(line):
                    span = next((group for group in match.groups() if group), "")
                    span = _clean_text(span, 100)
                    if not _is_safe_text(span):
                        continue
                    if any(noise in span for noise in DIALOGUE_NOISE):
                        continue
                    if not any(hint in span for hint in DIALOGUE_HINTS):
                        continue
                    key = span.strip("，。！？…~～ ")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    scored.append((self._dialogue_score(span), order, span))
                    order += 1
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored]

    async def _style_web_material(self, soft: bool = False) -> str:
        pool = SOFT_STYLE_QUERIES if soft else STYLE_QUERIES
        preferred = pool[0]
        others = [query for query in pool if query != preferred]
        queries = [preferred] + random.sample(others, k=len(others))
        best_results: list[dict[str, str]] = []
        best_dialogues: list[str] = []
        best_query = ""
        for query in queries[:2]:
            results = await self._web_search(query, need_content=True)
            if not results:
                continue
            dialogues = self._extract_dialogue_lines(results)
            if len(dialogues) > len(best_dialogues):
                best_results, best_dialogues = results, dialogues
                best_query = query
            if len(best_dialogues) >= 4:
                break
        if not best_results:
            return ""
        for item in best_results[:5]:
            self._record_source(
                "page",
                item.get("title") or "",
                item.get("url") or "",
                best_query,
                item.get("site") or "",
            )
        if best_dialogues:
            pool = best_dialogues[:24]
            picked = random.sample(pool, k=min(8, len(pool)))
            return "你顺带搜到一些小说/台词里的对话：\n" + "\n".join(
                f"- 「{line}」" for line in picked
            )
        # 没抽到带引号的对话时，退回短摘要，至少留一点素材。
        lines = [
            f"- {item.get('title') or ''} {item.get('snippet') or ''}".strip()
            for item in best_results
            if item.get("title") or item.get("snippet")
        ]
        if not lines:
            return ""
        return "你顺带搜到一些台词/小说片段：\n" + "\n".join(lines[:5])

    def _mark_surf_attempt(self, now: datetime, counted: bool) -> None:
        self._meta["last_surf_at"] = now.isoformat(timespec="seconds")
        if counted:
            if self._meta.get("surf_day") != now.date().isoformat():
                self._meta["surf_day"] = now.date().isoformat()
                self._meta["surf_count"] = 0
            self._meta["surf_count"] = int(self._meta.get("surf_count", 0) or 0) + 1
        self._save_meta()

    @staticmethod
    def _source_label(kind: str, site: str = "") -> str:
        if kind == "video":
            return "B站"
        return site or "网页"

    def _record_source(
        self,
        kind: str,
        title: str,
        url: str = "",
        query: str = "",
        site: str = "",
    ) -> None:
        """记下这次冲浪实际看过的页面/视频，只写日志，不进入聊天或提示词。"""
        if not self.cfg.get("surf_history_enabled", True):
            return
        title = _clean_text(str(title or ""), 120).strip()
        raw_url = str(url or "").strip()
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        match = re.match(r"https?://\S+", raw_url)
        url = match.group(0)[:300] if match else ""
        query = _clean_text(str(query or ""), 80).strip()
        site = _clean_text(str(site or ""), 40).strip()
        if not title and not url:
            return
        dedupe = (url or f"{kind}:{title}").lower()
        for item in self._surf_sources:
            other = (
                item.get("url") or f"{item.get('kind')}:{item.get('title')}"
            ).lower()
            if other == dedupe:
                return
        self._surf_sources.append(
            {"kind": kind, "title": title, "url": url, "query": query, "site": site}
        )
        if len(self._surf_sources) > 20:
            self._surf_sources = self._surf_sources[-20:]

    def _write_surf_corpus(
        self, now: datetime, material: str, diary: str, deep: bool = False
    ) -> None:
        """把娱乐冲浪的原始素材落盘，供兴趣偏好插件做知识卡。

        只写 fun 分支；学语气分支不落，避免语气素材被当成兴趣来源。
        """
        if not self.cfg.get("surf_corpus_enabled", True):
            return
        try:
            self._surf_corpus_dir.mkdir(parents=True, exist_ok=True)
            stamp = now.strftime("%Y%m%d-%H%M%S")
            stamp_iso = now.isoformat(timespec="seconds")
            path = self._surf_corpus_dir / f"{stamp}-fun.md"
            lines = ["# 冲浪素材", f"- 时间: {stamp_iso}", "- mode: fun"]
            if deep:
                lines.append("- deep: 1")
            lines.append("- 来源:")
            if self._surf_sources:
                for item in self._surf_sources:
                    label = self._source_label(
                        item.get("kind", ""), item.get("site", "")
                    )
                    line = f"  - [{label}] 《{item.get('title') or '无标题'}》"
                    if item.get("url"):
                        line += f" {item['url']}"
                    if item.get("query"):
                        line += f"（搜索词：{item['query']}）"
                    lines.append(line)
            else:
                lines.append("  - （这次没有解析出来源）")
            lines.append("")
            lines.append("## 日记")
            lines.append(str(diary or "").strip())
            lines.append("")
            lines.append("## 素材")
            lines.append(str(material or "").strip()[:6000])
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 写冲浪素材失败：{exc}")

    def _write_surf_history(
        self, now: datetime, style_mode: bool, diary: str
    ) -> None:
        mode = "学说话" if style_mode else "娱乐"
        sources = list(self._surf_sources)
        if self.cfg.get("surf_history_enabled", True):
            try:
                with self._surf_history_file.open("a", encoding="utf-8") as fh:
                    fh.write(
                        f"===== {now.strftime('%Y-%m-%d %H:%M:%S')} [{mode}] =====\n"
                    )
                    for item in sources:
                        label = self._source_label(
                            item.get("kind", ""), item.get("site", "")
                        )
                        line = f"- {label}《{item.get('title') or '无标题'}》"
                        if item.get("url"):
                            line += f" {item['url']}"
                        if item.get("query"):
                            line += f"（搜索词：{item['query']}）"
                        fh.write(line + "\n")
                    if not sources:
                        fh.write("- （这次没有解析出来源）\n")
                    fh.write(f"日记：{diary}\n\n")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[persona_growth] 写冲浪记录失败：{exc}")
        parts: list[str] = []
        for item in sources[:5]:
            title = (item.get("title") or "无标题").strip()
            if len(title) > 36:
                title = title[:36] + "…"
            piece = (
                f"{self._source_label(item.get('kind', ''), item.get('site', ''))}"
                f"《{title}》"
            )
            if item.get("url"):
                piece += f" {item['url']}"
            parts.append(piece)
        if len(sources) > 5:
            parts.append(f"等 {len(sources)} 个来源")
        logger.info(
            "[persona_growth] 这次冲浪（%s）看了：%s｜日记：%s",
            mode,
            "；".join(parts) if parts else "（没解析出来源）",
            diary[:60],
        )

    # ---------- B站游客刷视频 ----------

    def _bili_headers(self, buvid: str) -> dict[str, str]:
        headers = {
            "User-Agent": BILIBILI_UA,
            "Referer": BILIBILI_REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if buvid:
            headers["Cookie"] = f"buvid3={buvid}"
        return headers

    async def _bili_buvid(self, session: aiohttp.ClientSession) -> str:
        cached = str(self._meta.get("bilibili_buvid") or "")
        cached_at = self._parse_dt(self._meta.get("bilibili_buvid_at"))
        hours = max(1.0, float(self.cfg.get("bilibili_cache_hours", 12) or 12))
        if cached and cached_at and self._now() - cached_at < timedelta(hours=hours):
            return cached
        try:
            async with session.get(
                "https://api.bilibili.com/x/frontend/finger/spi",
                headers={
                    "User-Agent": BILIBILI_UA,
                    "Referer": BILIBILI_REFERER,
                },
            ) as response:
                if response.status != 200:
                    return cached
                data = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[persona_growth] B站访客 cookie 获取失败：{exc}")
            return cached
        b3 = str(((data or {}).get("data") or {}).get("b_3") or "")
        if not b3:
            return cached
        self._meta["bilibili_buvid"] = b3
        self._meta["bilibili_buvid_at"] = self._now().isoformat(timespec="seconds")
        self._save_meta()
        return b3

    async def _bili_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    logger.debug(
                        f"[persona_growth] B站接口 HTTP {response.status}：{url}"
                    )
                    return None
                data = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[persona_growth] B站接口失败：{url} {exc}")
            return None
        if not isinstance(data, dict) or int(data.get("code", -1)) != 0:
            return None
        return data

    async def _bilibili_material(
        self,
        keywords: list[str] | None = None,
        exclude_keys: set[str] | None = None,
    ) -> tuple[str, list[str]]:
        if not self.cfg.get("bilibili_enabled", True):
            return "", []
        if keywords:
            keywords = [str(item).strip() for item in keywords if str(item).strip()]
        else:
            keywords = self._clean_list(self.cfg.get("bilibili_keywords"), 30, 10)
            if not keywords:
                keywords = list(BILIBILI_KEYWORDS)
        if not keywords:
            return "", []
        keyword = random.choice(keywords)
        max_videos = max(1, min(5, int(self.cfg.get("bilibili_max_videos", 3) or 3)))
        danmaku_limit = max(
            5, min(60, int(self.cfg.get("bilibili_danmaku_lines", 30) or 30))
        )
        timeout = aiohttp.ClientTimeout(total=25)
        try:
            async with aiohttp.ClientSession(
                trust_env=True, timeout=timeout
            ) as session:
                buvid = await self._bili_buvid(session)
                headers = self._bili_headers(buvid)
                data = await self._bili_json(
                    session,
                    "https://api.bilibili.com/x/web-interface/search/type",
                    headers,
                    {
                        "search_type": "video",
                        "keyword": keyword,
                        "page": 1,
                        "order": "totalrank",
                    },
                )
                if not data:
                    return "", []
                raw_items = ((data.get("data") or {}).get("result")) or []
                videos: list[dict[str, str]] = []
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    bvid = str(item.get("bvid") or "").strip()
                    title = html.unescape(HTML_TAG_RE.sub("", str(item.get("title") or "")))
                    title = _clean_text(title, 80)
                    description = _clean_text(
                        html.unescape(
                            HTML_TAG_RE.sub("", str(item.get("description") or ""))
                        ),
                        120,
                    )
                    tag = _clean_text(
                        html.unescape(HTML_TAG_RE.sub("", str(item.get("tag") or ""))),
                        80,
                    )
                    if not bvid or not title:
                        continue
                    if not (
                        _is_bili_source_ok(title)
                        and (not description or _is_bili_source_ok(description))
                        and (not tag or _is_bili_source_ok(tag))
                    ):
                        continue
                    bili_key = self._ent_key("bili", bvid)
                    title_key = self._ent_key("bili-title", title)
                    if exclude_keys and (
                        bili_key in exclude_keys or title_key in exclude_keys
                    ):
                        continue
                    videos.append(
                        {
                            "bvid": bvid,
                            "title": title,
                            "desc": description,
                            "tag": tag,
                            "key": bili_key,
                            "title_key": title_key,
                        }
                    )
                    if len(videos) >= max_videos:
                        break
                if not videos:
                    return "", []

                blocks = [f"你刚在B站刷到「{keyword}」相关的视频："]
                danmaku: list[str] = []
                used_keys: list[str] = []
                for video in videos:
                    used_keys.extend(
                        [video.get("key") or "", video.get("title_key") or ""]
                    )
                    info = await self._bili_json(
                        session,
                        "https://api.bilibili.com/x/web-interface/view",
                        headers,
                        {"bvid": video["bvid"]},
                    )
                    cid = 0
                    if info:
                        detail = info.get("data") or {}
                        cid = int(detail.get("cid") or 0)
                        desc = _clean_text(
                            html.unescape(
                                HTML_TAG_RE.sub("", str(detail.get("desc") or ""))
                            ),
                            120,
                        )
                        if desc and _is_bili_source_ok(desc):
                            video["desc"] = desc
                        tags = [
                            str(t.get("tag_name") or "")
                            for t in (detail.get("tags") or [])
                            if isinstance(t, dict)
                        ]
                        if not tags:
                            tag_data = await self._bili_json(
                                session,
                                "https://api.bilibili.com/x/tag/archive/tags",
                                headers,
                                {"bvid": video["bvid"]},
                            )
                            if tag_data:
                                tags = [
                                    str(t.get("tag_name") or "")
                                    for t in (tag_data.get("data") or [])
                                    if isinstance(t, dict)
                                ]
                        tag_text = " ".join(
                            _clean_text(t, 20) for t in tags[:6] if t
                        )
                        if tag_text and _is_bili_source_ok(tag_text):
                            video["tag"] = tag_text

                    block = f"- {video['title']}"
                    if video.get("desc"):
                        block += f"｜简介：{video['desc']}"
                    if video.get("tag"):
                        block += f"｜标签：{video['tag']}"
                    blocks.append(block)

                    if cid:
                        danmaku.extend(
                            await self._bili_danmaku(session, headers, cid)
                        )

                if danmaku:
                    random.shuffle(danmaku)
                    blocks.append("弹幕里有人这么说（只是参考，别照抄）：")
                    blocks.append("｜".join(danmaku[:danmaku_limit]))
                for video in videos:
                    self._record_source(
                        "video",
                        video.get("title") or "",
                        f"https://www.bilibili.com/video/{video['bvid']}",
                        keyword,
                    )
                return "\n".join(blocks)[-3500:], used_keys
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] B站刷视频失败：{exc}")
            return "", []

    async def _bili_danmaku(
        self,
        session: aiohttp.ClientSession,
        headers: dict[str, str],
        cid: int,
    ) -> list[str]:
        try:
            async with session.get(
                f"https://comment.bilibili.com/{cid}.xml", headers=headers
            ) as response:
                if response.status != 200:
                    return []
                raw = await response.read()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[persona_growth] 弹幕获取失败：{exc}")
            return []
        text = self._decode_danmaku(raw)
        if not text:
            return []
        lines: list[str] = []
        for match in re.finditer(r"<d[^>]*>(.*?)</d>", text, flags=re.S):
            line = html.unescape(match.group(1))
            line = _clean_text(line, 50)
            if len(line) < 3 or not _is_bili_source_ok(line):
                continue
            if not _is_interesting_danmaku(line):
                continue
            lines.append(line)
        random.shuffle(lines)
        return lines[:40]

    @staticmethod
    def _decode_danmaku(raw: bytes) -> str:
        if raw[:5].lower().startswith(b"<?xml"):
            return raw.decode("utf-8", "ignore")
        for wbits in (zlib.MAX_WBITS | 32, -zlib.MAX_WBITS, zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def _web_search(
        self, query: str, need_content: bool = False
    ) -> list[dict[str, str]]:
        keys = self._web_keys()
        if not keys:
            return []
        payload = {
            "query": query[:128],
            "count": 5,
            "need_content": need_content,
        }
        data: dict[str, Any] | None = None
        for _ in range(min(len(keys), 3)):
            key = keys[self._web_key_index % len(keys)]
            self._web_key_index += 1
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            try:
                timeout = aiohttp.ClientTimeout(total=45 if need_content else 30)
                async with aiohttp.ClientSession(
                    trust_env=True, timeout=timeout
                ) as session:
                    async with session.post(
                        "https://api.deepseek.com/v1/tools/search",
                        json=payload,
                        headers=headers,
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            break
                        if response.status in (401, 403, 429):
                            continue
                        return []
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[persona_growth] 冲浪搜索失败：{exc}")
                return []
        if data is None:
            return []

        results: list[dict[str, str]] = []
        for item in (data.get("results") or [])[:5]:
            if not isinstance(item, dict):
                continue
            title = _clean_text(str(item.get("title") or ""), 80)
            snippet = _clean_text(
                str(item.get("summary") or item.get("snippet") or ""), 200
            )
            content = ""
            if need_content:
                content = str(item.get("content") or "")[:6000]
            url = str(item.get("url") or item.get("link") or "").strip()
            site = _clean_text(str(item.get("site_name") or ""), 40)
            if title or snippet or content:
                results.append(
                    {
                        "title": title,
                        "snippet": snippet,
                        "content": content,
                        "url": url,
                        "site": site,
                    }
                )
        return results

    def _web_keys(self) -> list[str]:
        try:
            settings = self.context.get_config().get("provider_settings", {}) or {}
        except Exception:  # noqa: BLE001
            return []
        keys = settings.get("websearch_deepseek_key") or []
        if isinstance(keys, str):
            keys = [keys]
        return [str(key) for key in keys if str(key).strip()]

    # ---------- 网页深读与知识卡 ----------

    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"[^一-鿿a-zA-Z0-9]", "", str(text or "")).lower()

    @staticmethod
    def _valid_deep_url(url: str) -> bool:
        raw = str(url or "").strip()
        if not raw or len(raw) > 8192 or any(ch.isspace() for ch in raw):
            return False
        try:
            parsed = urlsplit(raw)
            _ = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    async def _fetch_page(self, url: str) -> dict[str, str] | None:
        """用 DeepSeek Fetch 把网页抓成 Markdown；失败就返回 None。"""
        if not self._valid_deep_url(url):
            return None
        keys = self._web_keys()
        if not keys:
            return None
        timeout = max(
            10.0, float(self.cfg.get("deep_read_fetch_timeout", 30) or 30)
        )
        for _ in range(min(len(keys), 3)):
            key = keys[self._web_key_index % len(keys)]
            self._web_key_index += 1
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            try:
                async with aiohttp.ClientSession(
                    trust_env=True, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.post(
                        "https://api.deepseek.com/v1/tools/fetch",
                        json={"url": url},
                        headers=headers,
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            content = str((data or {}).get("content") or "")
                            if content.strip():
                                return {
                                    "title": str((data or {}).get("title") or ""),
                                    "content": content,
                                }
                            return None
                        if response.status in (401, 403, 429):
                            continue
                        return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[persona_growth] 深读抓取失败：{url} {exc}")
                return None
        return None

    def _clean_page_content(self, markdown: str) -> str:
        """去掉导航、广告和链接，只留正文；太长时保留头尾。"""
        text = html.unescape(str(markdown or ""))
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = HTML_TAG_RE.sub("\n", text)
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = re.sub(r"^[#>*\-\s]+", "", raw_line).strip()
            line = re.sub(r"\s+", " ", line)
            if len(line) < 15:
                continue
            folded = line.casefold()
            if any(noise in folded for noise in DEEP_NOISE_LINES):
                continue
            lines.append(line)
        body = "\n".join(lines).strip()
        limit = max(1500, int(self.cfg.get("deep_read_max_chars", 6000) or 6000))
        if len(body) <= limit:
            return body
        head = int(limit * 0.65)
        tail = limit - head
        return body[:head] + "\n……\n" + body[-tail:]

    def _rank_deep_urls(self, results: list[dict[str, str]]) -> list[dict[str, str]]:
        seen = set(str(item) for item in (self._meta.get("seen_deep_urls") or []))
        ranked: list[tuple[float, dict[str, str]]] = []
        for item in results:
            url = str(item.get("url") or "").strip()
            if not self._valid_deep_url(url) or url in seen:
                continue
            folded = url.casefold()
            if any(block in folded for block in DEEP_DOMAIN_BLOCK):
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            if not title and not snippet:
                continue
            score = 0.0
            for domain, bonus in DEEP_DOMAIN_BONUS.items():
                if domain in folded:
                    score += bonus
                    break
            score += min(len(snippet), 200) / 100.0
            if title:
                score += 1.0
            ranked.append((score, item))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [item for _, item in ranked]

    def _pick_deep_url(self, results: list[dict[str, str]]) -> dict[str, str] | None:
        ranked = self._rank_deep_urls(results)
        return ranked[0] if ranked else None

    async def _distill_knowledge(
        self, content: str, topic_hint: str = ""
    ) -> dict[str, Any] | None:
        """整理员：把正文压成 2-3 条带出处的事实。"""
        provider_id = str(self.cfg.get("provider_id") or "")
        if not provider_id or not content.strip():
            return None
        prompt = content[:6000]
        if topic_hint:
            prompt = f"这次的主题大概是「{topic_hint}」。\n\n{prompt}"
        raw = ""
        for attempt in range(2):
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=KNOWLEDGE_DISTILL_PROMPT,
                    prompt=prompt
                    + (
                        "\n\n上次的 src 在正文里找不到。这次只输出能在正文找到原句的 facts。"
                        if attempt
                        else ""
                    ),
                    temperature=0.2,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[persona_growth] 整理员调用失败：{exc}")
                return None
            raw = str(getattr(response, "completion_text", "") or "")
            data = self._parse_json_object(raw)
            if not data:
                continue
            topic = _clean_text(str(data.get("topic") or ""), 12).strip()
            if not topic:
                continue
            content_norm = self._norm_text(content)
            content_grams = self._bigrams(content_norm)
            facts: list[dict[str, str]] = []
            for item in data.get("facts") or []:
                if not isinstance(item, dict):
                    continue
                fact = _clean_text(str(item.get("text") or ""), 80).strip()
                src = _clean_text(str(item.get("src") or ""), 80).strip()
                if len(fact) < 6 or len(src) < 4:
                    continue
                src_norm = self._norm_text(src)
                if src_norm not in content_norm:
                    # 模型偶尔会把 src 改一两个字；二元组覆盖率 ≥85% 才算原文的近似。
                    src_grams = self._bigrams(src_norm)
                    if (
                        len(src_norm) < 6
                        or not src_grams
                        or len(src_grams & content_grams) / len(src_grams) < 0.85
                    ):
                        continue
                if not _is_safe_text(fact) or not _is_safe_text(src):
                    continue
                facts.append({"text": fact, "src": src})
                if len(facts) >= 3:
                    break
            if len(facts) < 2:
                continue
            quotes: list[str] = []
            for item in data.get("quotes") or []:
                quote = _clean_text(str(item), 80).strip()
                if (
                    len(quote) >= 4
                    and self._norm_text(quote) in content_norm
                    and _is_safe_text(quote)
                ):
                    quotes.append(quote)
                if len(quotes) >= 2:
                    break
            return {
                "topic": topic,
                "facts": facts,
                "why": _clean_text(str(data.get("why") or ""), 60).strip(),
                "quotes": quotes,
            }
        return None

    async def _deep_read_material(
        self, topic_hint: str, seen: set[str]
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        """搜索一篇正文 → Fetch → 知识卡。失败时返回空，由调用方降级。"""
        if not self.cfg.get("deep_read_enabled", True):
            return None, "", []
        pool = [topic_hint] if topic_hint else []
        pool += list(ENTERTAINMENT_TOPICS)
        random.shuffle(pool)
        for topic in pool[:2]:
            query = f"{topic} 是什么 科普 介绍"
            results = await self._web_search(query, need_content=False)
            # 一次 fetch 可能被站点拒（实测有 504），换第二候选再试。
            for item in self._rank_deep_urls(results)[:2]:
                page = await self._fetch_page(str(item.get("url") or ""))
                if not page:
                    continue
                content = self._clean_page_content(page["content"])
                if len(content) < 500:
                    continue
                card = await self._distill_knowledge(content, topic)
                if not card:
                    continue
                title = _clean_text(page.get("title") or item.get("title") or "", 80)
                self._record_source(
                    "page",
                    title,
                    str(item.get("url") or ""),
                    query,
                    item.get("site") or "",
                )
                card["source"] = {
                    "title": title,
                    "url": str(item.get("url") or ""),
                    "query": query,
                }
                self._remember_deep_url(str(item.get("url") or ""))
                lines = [
                    f"你刚读到一篇讲「{card['topic']}」的文章《{title}》：",
                ]
                for fact in card["facts"][:3]:
                    lines.append(f"- {fact['text']}")
                if card.get("why"):
                    lines.append(f"- 你的感受：{card['why']}")
                if card.get("quotes"):
                    lines.append(
                        "原文里有这两句："
                        + "｜".join(f"「{quote}」" for quote in card["quotes"][:2])
                    )
                material = "\n".join(lines)
                return card, material, [str(item.get("url") or "")]
        return None, "", []

    def _seen_deep_urls(self) -> list[str]:
        raw = self._meta.get("seen_deep_urls")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def _remember_deep_url(self, url: str) -> None:
        if not url:
            return
        items = self._seen_deep_urls()
        if url not in items:
            items.append(url)
        self._meta["seen_deep_urls"] = items[-200:]
        self._save_meta()

    def _load_knowledge(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        try:
            lines = self._knowledge_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return cards
        for line in lines[-500:]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("topic") and item.get("facts"):
                cards.append(item)
        return cards[-int(self.cfg.get("knowledge_max_cards", 300) or 300) :]

    def _save_knowledge(self) -> None:
        try:
            self._knowledge_file.parent.mkdir(parents=True, exist_ok=True)
            with self._knowledge_file.open("w", encoding="utf-8") as handle:
                for card in self._knowledge_cards:
                    handle.write(json.dumps(card, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[persona_growth] 写知识卡失败：{exc}")

    def _append_knowledge(self, card: dict[str, Any]) -> None:
        topic = str(card.get("topic") or "").strip()
        facts = [item.get("text") for item in card.get("facts") or [] if item.get("text")]
        if not topic or len(facts) < 2:
            return
        # 同一主题、同一条事实不重复记。
        for old in self._knowledge_cards[-100:]:
            if str(old.get("topic") or "") != topic:
                continue
            old_texts = {
                self._norm_text(str(item.get("text") or ""))
                for item in old.get("facts") or []
            }
            if any(self._norm_text(str(text)) in old_texts for text in facts):
                return
        now = self._now()
        record = dict(card)
        record["id"] = f"{now.strftime('%Y%m%d-%H%M%S')}-{random.randrange(1000, 9999)}"
        record["at"] = now.isoformat(timespec="seconds")
        self._knowledge_cards.append(record)
        limit = int(self.cfg.get("knowledge_max_cards", 300) or 300)
        if len(self._knowledge_cards) > limit:
            self._knowledge_cards = self._knowledge_cards[-limit:]
        self._save_knowledge()

    def _match_knowledge(
        self, text: str, cards: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        hay = str(text or "").lower()
        if not hay.strip():
            return []
        cards = cards if cards is not None else self._knowledge_cards
        grams = self._bigrams(hay)
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for card in cards:
            topic = str(card.get("topic") or "").strip()
            if not topic:
                continue
            names = [topic] + list(KNOWLEDGE_ALIASES.get(topic, []))
            strong = any(name.lower() in hay for name in names if name)
            weak = len(grams & self._bigrams(topic)) >= 2
            if strong or weak:
                scored.append((2 if strong else 1, str(card.get("at") or ""), card))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [row[2] for row in scored[: int(self.cfg.get("knowledge_inject_max", 2) or 2)]]

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        clean = re.sub(r"[^一-鿿a-zA-Z0-9]", "", str(text or "").lower())
        if len(clean) < 2:
            return {clean} if clean else set()
        return {clean[i : i + 2] for i in range(len(clean) - 1)}

    def _knowledge_block(self, cards: list[dict[str, Any]]) -> str:
        if not cards:
            return ""
        lines: list[str] = []
        budget = max(80, int(self.cfg.get("knowledge_inject_chars", 180) or 180))
        used = 0
        for card in cards:
            topic = str(card.get("topic") or "").strip()
            facts = [
                _clean_text(str(item.get("text") or ""), 60).strip()
                for item in card.get("facts") or []
                if item.get("text")
            ][:2]
            if not topic or not facts:
                continue
            line = f"- {topic}：" + "；".join(facts)
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        return (
            "【你最近知道的事】\n"
            + "\n".join(lines)
            + "\n（知道就说一句，不确定就别展开；不要背资料，不要提「我学过/我知道」。）"
        )

    async def _knowledge_context(self, event: AstrMessageEvent) -> str:
        """命中主题才查知识库；查不到就退回本地知识卡。"""
        if not self.cfg.get("knowledge_enabled", True):
            return ""
        text = str(getattr(event, "message_str", "") or "")
        matched = self._match_knowledge(text)
        if not matched:
            return ""
        # 非向量路线：只读本地 knowledge.jsonl，不碰 kb_manager。
        backend = str(self.cfg.get("knowledge_backend") or "auto").strip().lower()
        if backend == "local":
            return self._knowledge_block(matched)
        kb_name = str(self.cfg.get("knowledge_kb_name") or "").strip()
        if kb_name:
            kb_mgr = getattr(self.context, "kb_manager", None)
            if kb_mgr is not None:
                try:
                    kb = await kb_mgr.get_kb_by_name(kb_name)
                except Exception:  # noqa: BLE001
                    kb = None
                # 空库不触发嵌入，避免把本机 Ollama 的模型无谓叫醒。
                if kb is None or not (
                    getattr(kb.kb, "doc_count", 0)
                    or getattr(kb.kb, "chunk_count", 0)
                ):
                    # vector 模式不回本地；auto 模式才兜底。
                    return "" if backend == "vector" else self._knowledge_block(matched)
                try:
                    result = await asyncio.wait_for(
                        kb_mgr.retrieve(
                            query=text[:300],
                            kb_names=[kb_name],
                            top_k_fusion=10,
                            top_m_final=3,
                        ),
                        timeout=6.0,
                    )
                    rows = result.get("results") if isinstance(result, dict) else None
                    if isinstance(rows, list) and rows:
                        lines: list[str] = []
                        used = 0
                        budget = max(
                            80, int(self.cfg.get("knowledge_inject_chars", 180) or 180)
                        )
                        for row in rows[:2]:
                            if not isinstance(row, dict):
                                continue
                            content = _clean_text(
                                str(row.get("content") or ""), 120
                            ).strip()
                            if not content:
                                continue
                            line = f"- {content}"
                            if used + len(line) + 1 > budget:
                                break
                            lines.append(line)
                            used += len(line) + 1
                        if lines:
                            return (
                                "【你最近知道的事】\n"
                                + "\n".join(lines)
                                + "\n（知道就说一句，不确定就别展开；不要背资料，不要提「我学过/我知道」。）"
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[persona_growth] 知识库检索失败：{exc}")
        # vector 模式只认知识库；auto 模式没查到再回本地。
        return "" if backend == "vector" else self._knowledge_block(matched)

    @staticmethod
    def _clean_list(raw: Any, max_len: int, limit: int) -> list[str]:
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for item in raw:
            text = _clean_text(str(item), max_len)
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        return result

    # ---------- 发送前润色 ----------

    @filter.on_decorating_result()
    async def polish_reply(self, event: AstrMessageEvent) -> None:
        if not self.cfg.get("enabled", True) or not self.cfg.get("style_cleanup", True):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain:
            return
        for component in chain:
            text = getattr(component, "text", None)
            if not isinstance(text, str) or not text.strip():
                continue
            if "```" in text or len(text) > 800:
                continue
            cleaned = self._clean_reply(text)
            if cleaned and cleaned != text:
                try:
                    component.text = cleaned
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _clean_reply(text: str) -> str:
        cleaned = BOLD_RE.sub(r"\1", text)
        cleaned = UNDERSCORE_RE.sub(r"\1", cleaned)
        cleaned = CODE_TICK_RE.sub(r"\1", cleaned)
        cleaned = HEADING_RE.sub("", cleaned)
        for phrase in ASSISTANT_PHRASES:
            cleaned = cleaned.replace(phrase, "")
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = re.sub(r"。{2,}", "。", cleaned)
        return cleaned.strip() or text.strip()

    # ---------- 小工具 ----------

    def _parse_dt(self, raw: Any) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self._tz)
        return moment.astimezone(self._tz)


__all__ = ["Main"]
