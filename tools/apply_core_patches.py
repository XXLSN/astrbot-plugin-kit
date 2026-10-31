#!/usr/bin/env python3
"""应用 / 恢复 AstrBot 本体补丁（core-patches/）。

这些补丁只适用于 manifest.json 里记录的 AstrBot 版本。默认会做版本检查，
版本不一致时拒绝执行；确实要试可以加 --force。

用法：
    # 1. 先看会改哪些文件
    python tools/apply_core_patches.py --astrbot-home "C:/Users/me/AppData/Local/AstrBot" --dry-run

    # 2. 停掉 AstrBot 后正式应用（会自动备份原文件）
    python tools/apply_core_patches.py --astrbot-home "C:/Users/me/AppData/Local/AstrBot"

    # 3. 出问题就恢复（把 <备份目录> 换成脚本打印出来的那个）
    python tools/apply_core_patches.py --astrbot-home "C:/Users/me/AppData/Local/AstrBot" --restore "<备份目录>"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
PATCH_ROOT = PKG_ROOT / "core-patches"
MANIFEST = PATCH_ROOT / "manifest.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def default_home() -> Path:
    env = os.environ.get("ASTRBOT_HOME") or os.environ.get("ASTRBOT_APP_HOME")
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "AstrBot"
    return Path.home() / "AstrBot"


def read_version(home: Path) -> str:
    init = home / "backend" / "app" / "astrbot" / "__init__.py"
    try:
        text = init.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1).strip() if m else ""


def load_manifest() -> dict:
    if not MANIFEST.exists():
        raise SystemExit(f"找不到 {MANIFEST}")
    return json.loads(MANIFEST.read_text(encoding="utf-8-sig"))


def find_python(home: Path) -> Path | None:
    """优先用 AstrBot 自带的 Python 做语法检查（系统 Python 可能太旧）。"""
    for rel in (
        "backend/python/python.exe",
        "backend/python/bin/python3",
        "backend/python/bin/python",
    ):
        cand = home / rel
        if cand.exists():
            return cand
    return None


def compile_files(paths: list[Path], home: Path) -> bool:
    py_paths = [p for p in paths if p.suffix == ".py"]
    if not py_paths:
        return True
    python = find_python(home)
    if python is not None:
        result = subprocess.run(
            [str(python), "-m", "py_compile", *[str(p) for p in py_paths]],
            capture_output=True,
            text=True,
        )
        ok = result.returncode == 0
        if not ok:
            print((result.stderr or result.stdout or "").strip())
    else:
        # 系统 Python 可能是 3.9，遇到 match 语法会误报；这种情况只提醒，不阻断。
        ok = True
        for path in py_paths:
            try:
                py_compile.compile(str(path), doraise=True)
            except py_compile.PyCompileError as exc:
                print(f"[提醒] 当前 Python 无法校验 {path}（可能是版本太旧）：{exc}")
        print("[提醒] 没有找到 AstrBot 自带的 Python，跳过严格语法检查。")
    for cache in {p.parent for p in py_paths}:
        shutil.rmtree(cache / "__pycache__", ignore_errors=True)
    return ok


def apply(home: Path, manifest: dict, dry: bool, force: bool, do_compile: bool) -> int:
    target_root = home / "backend" / "app" / "astrbot"
    if not target_root.is_dir():
        print(f"[错误] 找不到 {target_root}")
        print("请用 --astrbot-home 指向 AstrBot 安装目录（里面应有 backend/app/astrbot）。")
        return 1

    expected = str(manifest.get("source_astrbot_version") or "")
    actual = read_version(home)
    print(f"AstrBot 安装目录：{home}")
    print(f"当前版本：{actual or '读不到'}；补丁适用版本：{expected or '未记录'}")
    if expected and actual != expected and not force:
        print("\n[拒绝] 版本不一致。")
        print("这些补丁是按上面记录的版本改的，跨版本直接覆盖可能把 AstrBot 弄坏。")
        print("确认要试，加 --force；更推荐先找原作者要对应版本的补丁。")
        return 2

    entries = manifest.get("files") or []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = home / f"core-patch-backup-{stamp}"
    changed: list[Path] = []

    for entry in entries:
        rel = str(entry.get("path") or "")
        src = PATCH_ROOT / rel
        dst = home / rel
        if not src.exists():
            print(f"[跳过] 包里缺文件：{rel}")
            continue
        if dst.exists() and sha256(dst) == entry.get("sha256"):
            print(f"[已是补丁] {rel}")
            continue
        print(f"[{'预览' if dry else '应用'}] {rel}")
        if dry:
            continue
        if dst.exists():
            backup = backup_dir / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, backup)
        else:
            print(f"  [提醒] 目标原本不存在，将新建：{rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        changed.append(dst)

    if dry:
        print("\n[dry-run] 没有写入任何文件。")
        return 0

    if changed:
        print(f"\n已修改 {len(changed)} 个文件。")
        print(f"原文件备份在：{backup_dir}")
        if do_compile and not compile_files(changed, home):
            print("\n[警告] 有文件语法检查失败。建议立刻用 --restore 恢复。")
            return 3
    else:
        print("\n所有文件都已经是补丁版本，无需修改。")

    print("\n下一步：重启 AstrBot，然后按 core-patches/使用说明.md 的验收清单逐项检查。")
    return 0


def restore(home: Path, manifest: dict, backup_dir: Path, do_compile: bool) -> int:
    if not backup_dir.is_dir():
        print(f"[错误] 备份目录不存在：{backup_dir}")
        return 1
    restored: list[Path] = []
    for entry in manifest.get("files") or []:
        rel = str(entry.get("path") or "")
        src = backup_dir / rel
        dst = home / rel
        if not src.exists():
            print(f"[跳过] 备份里没有：{rel}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(dst)
        print(f"[恢复] {rel}")
    if not restored:
        print("没有恢复任何文件。")
        return 1
    if do_compile:
        compile_files(restored, home)
    print(f"\n已恢复 {len(restored)} 个文件。重启 AstrBot 生效。")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="应用/恢复 AstrBot 本体补丁")
    ap.add_argument("--astrbot-home", default=str(default_home()),
                    help="AstrBot 安装目录（含 backend/app），默认 %%LOCALAPPDATA%%\\AstrBot")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    ap.add_argument("--force", action="store_true", help="版本不一致也继续（有风险）")
    ap.add_argument("--no-compile", action="store_true", help="跳过语法检查")
    ap.add_argument("--restore", default="", help="从指定备份目录恢复")
    args = ap.parse_args()

    home = Path(args.astrbot_home).expanduser().resolve()
    manifest = load_manifest()

    if args.restore:
        return restore(home, manifest, Path(args.restore).expanduser().resolve(), not args.no_compile)
    return apply(home, manifest, args.dry_run, args.force, not args.no_compile)


if __name__ == "__main__":
    raise SystemExit(main())
