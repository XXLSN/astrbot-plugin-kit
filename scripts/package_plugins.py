#!/usr/bin/env python3
"""生成发布用压缩包：每个插件一个独立 zip，另加一个整包 zip 和 SHA256。

用法：
    python scripts/package_plugins.py
输出目录：
    dist/
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
DIST = ROOT / "dist"
PACKAGE_NAME = ROOT.name

SKIP_DIRS = {".git", "dist", "__pycache__", "data", "msgs", "surf_corpus"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".bak"}
SKIP_NAMES = {
    "diary.json", "memories.json", "key_facts.json", "prefs.json",
    "state.json", "ledger.json", "proficiency.json", "knowledge.jsonl",
    "sources.jsonl", "events.jsonl", "score_log.jsonl", "decisions.jsonl",
    "cmd_config.json",
}


def iter_files(base: Path):
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        yield path, rel


def write_zip(out: Path, base: Path, arc_root: str) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in iter_files(base):
            info = zipfile.ZipInfo(str(Path(arc_root) / rel))
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if not PLUGINS.is_dir():
        raise SystemExit("找不到 plugins/ 目录")
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("*.zip"):
        old.unlink()

    assets: list[Path] = []
    for plugin in sorted(p for p in PLUGINS.iterdir() if p.is_dir()):
        out = DIST / f"{plugin.name}.zip"
        write_zip(out, plugin, plugin.name)
        assets.append(out)
        print(f"{out.name}: {out.stat().st_size} bytes")

    full = DIST / "astrbot-plugin-kit.zip"
    write_zip(full, ROOT, PACKAGE_NAME)
    assets.append(full)
    print(f"{full.name}: {full.stat().st_size} bytes")

    sums = DIST / "SHA256SUMS.txt"
    sums.write_text(
        "".join(f"{sha256(p)}  {p.name}\n" for p in assets),
        encoding="utf-8",
    )
    print(f"{sums.name}: {len(assets)} assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
