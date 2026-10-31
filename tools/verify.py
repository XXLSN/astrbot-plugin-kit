#!/usr/bin/env python3
"""发布前自检：确认包里没有本机绝对路径、运行时数据文件，插件都能编译。

用法：python tools/verify.py
如需额外拦截自己的 QQ/群号等字符串，用环境变量传入（逗号分隔）：
    set NUONUO_PRIVATE_STRINGS=123456789,987654321
    python tools/verify.py
"""

from __future__ import annotations

import json
import os
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

USER_PATH_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/]([^\\/\s\"'`]+)", re.I)
PLACEHOLDER_USERS = {
    "me", "user", "username", "you", "yourname",
    "<你>", "<用户名>", "<user>", "<username>", "%USERNAME%",
}
BAD_PATTERNS = [
    (re.compile(r"[A-Za-z]:[\\/]AstrBotData[\\/]", re.I), "本机绝对路径"),
]
EXTRA_BAD = [
    item.strip()
    for item in os.environ.get("NUONUO_PRIVATE_STRINGS", "").split(",")
    if item.strip()
]

BAD_FILES = {
    "diary.json",
    "meta.json",
    "turns.jsonl",
    "state.json",
    "prefs.json",
    "events.jsonl",
    "score_log.jsonl",
    "ledger.json",
    "decisions.jsonl",
    "knowledge.jsonl",
    "surf_history.log",
    "cmd_config.json",
    "memories.json",
    "key_facts.json",
}

BAD_DIRS = {"msgs", "surf_corpus", "__pycache__", "data"}
BAD_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".bak", ".log", ".pyc"}

failures: list[str] = []
warnings: list[str] = []


def check_text(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"读不了 {path.relative_to(ROOT)}：{exc}")
        return
    for pattern, label in BAD_PATTERNS:
        if pattern.search(text):
            failures.append(f"{path.relative_to(ROOT)} 含{label}")
    for match in USER_PATH_RE.finditer(text):
        user = match.group(1)
        if user.lower() in PLACEHOLDER_USERS or user.startswith(("<", "%")):
            continue
        failures.append(f"{path.relative_to(ROOT)} 含本机用户目录：{match.group(0)}")
        break
    for bad in EXTRA_BAD:
        if bad in text:
            failures.append(f"{path.relative_to(ROOT)} 含敏感字符串：{bad}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not PLUGINS.is_dir():
        print("找不到 plugins/ 目录")
        return 1

    self_path = Path(__file__).resolve()
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if path.resolve() == self_path:
            continue  # 本文件里就写着这些敏感字符串，跳过自己
        if path.is_dir():
            if path.name in BAD_DIRS:
                failures.append(f"不该打包的目录：{rel}")
            continue
        if path.name in BAD_FILES:
            failures.append(f"不该打包的数据文件：{rel}")
        if path.suffix.lower() in BAD_SUFFIXES:
            failures.append(f"不该打包的文件类型：{rel}")
        if path.suffix.lower() in {".py", ".json", ".yaml", ".yml", ".md", ".txt"}:
            check_text(path)

    for d in sorted(PLUGINS.iterdir()):
        if not d.is_dir():
            continue
        main_py = d / "main.py"
        meta = d / "metadata.yaml"
        if not main_py.exists():
            failures.append(f"{d.name} 缺 main.py")
            continue
        if not meta.exists():
            failures.append(f"{d.name} 缺 metadata.yaml")
        else:
            m = re.search(r"^name:\s*(\S+)", meta.read_text(encoding="utf-8-sig"), re.M)
            if not m or m.group(1) != d.name:
                failures.append(f"{d.name} 的 metadata name 不匹配：{m.group(1) if m else '无'}")
        cfg = d / "config.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{d.name}/config.json 不是合法 JSON：{exc}")
                continue
            for key, value in data.items():
                if isinstance(value, str) and re.match(r"^[A-Za-z]:[\\/]", value):
                    failures.append(f"{d.name}/config.json 还有绝对路径 {key}={value}")
        try:
            py_compile.compile(str(main_py), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"{d.name} 编译失败：{exc}")

    for cache in ROOT.rglob("__pycache__"):
        import shutil
        shutil.rmtree(cache, ignore_errors=True)

    print("=" * 60)
    print(f"插件数量：{len([d for d in PLUGINS.iterdir() if d.is_dir()])}")
    if warnings:
        print("\n提醒：")
        for w in warnings:
            print("  -", w)
    if failures:
        print("\n发现问题：")
        for f in failures:
            print("  ✗", f)
        print("\n自检失败。")
        return 1
    print("\n自检通过：没有本机绝对路径 / 运行时数据，全部插件可编译。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
