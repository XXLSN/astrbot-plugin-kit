#!/usr/bin/env python3
"""AstrBot 插件包安装器（Windows / macOS / Linux 通用，只用标准库）。

用法示例：
    python tools/install.py --owner-qq 123456789 --group-ids 111111,222222
    python tools/install.py --astrbot-data "C:/Users/me/.astrbot/data" --owner-qq 123456789

它做三件事：
1. 把 plugins/ 下的插件目录复制到 <AstrBot数据目录>/plugins/
2. 按你的 AstrBot 数据目录生成每个插件的 config.json（不含任何原作者的路径/QQ/群号）
3. 建好 plugin_data 下的数据目录，并做一次语法检查

不会改动 AstrBot 本体，也不会联网。
"""

from __future__ import annotations

import argparse
import json
import os
import py_compile
import shutil
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_SRC = PKG_ROOT / "plugins"

ALL_PLUGINS = [
    "astrbot_plugin_context_slim",
    "astrbot_plugin_cross_group",
    "astrbot_plugin_group_proactive",
    "astrbot_plugin_interest_profile",
    "astrbot_plugin_persona_growth",
    "astrbot_plugin_qq_profile",
    "astrbot_plugin_reply_pacing",
    "astrbot_plugin_self_memory",
    "astrbot_plugin_topic_proficiency",
    "astrbot_plugin_usage_cost",
]

NAME_VARIANTS = [
    "糯叽叽",
    "小糯米",
    "糯糯酱",
    "糯糯",
    "小糯",
    "糯酱",
    "糯宝",
    "阿糯",
    "糯米",
    "糯团",
]


def default_data_dir() -> Path:
    root = os.environ.get("ASTRBOT_ROOT")
    if root:
        return Path(root).expanduser() / "data"
    return Path.home() / ".astrbot" / "data"


def parse_list(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").replace("，", ",").split(",") if x.strip()]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_plugins(selected: list[str], plugin_dir: Path, force: bool, dry: bool) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for name in selected:
        src = PLUGINS_SRC / name
        dst = plugin_dir / name
        if not src.is_dir():
            print(f"[跳过] 包里没有 {name}")
            continue
        if dst.exists():
            if not force:
                print(f"[保留] {name} 已存在（要覆盖请加 --force）")
                continue
            if not dry:
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
        print(f"[复制] {name} -> {dst}")
        if dry:
            continue
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.bak", "*.bak-*"),
        )
        # 如果原插件目录里有旧的个人数据，绝不复制：上面只复制包内文件，
        # 包内已经排除了 config.json.bak / diary.json / state.json / msgs 等。


def rename_names(plugin_dir: Path, selected: list[str], bot_name: str, dry: bool) -> None:
    """把代码/元数据里的默认名字换成使用者自己的。只在 bot_name 不是「糯糯」时执行。"""
    bot_name = (bot_name or "").strip()
    if not bot_name or bot_name == "糯糯":
        return
    for name in selected:
        d = plugin_dir / name
        if not d.is_dir():
            continue
        for path in list(d.glob("*.py")) + list(d.glob("*.yaml")) + list(d.glob("*.json")):
            try:
                text = path.read_text(encoding="utf-8-sig")
            except Exception:  # noqa: BLE001
                continue
            new = text
            # 长的先换，并用占位符隔开，避免新名字里又含旧名字导致二次替换
            # （例如把「糯糯」换成「小糯米」时，不会再把「糯米」换一遍）。
            token = "\x00BOT_NAME\x00"
            for variant in NAME_VARIANTS:
                new = new.replace(variant, token)
            new = new.replace(token, bot_name)
            if new != text and not dry:
                path.write_text(new, encoding="utf-8")
                print(f"[改名] {path.relative_to(plugin_dir)}")


def build_configs(
    selected: list[str],
    plugin_dir: Path,
    data_dir: Path,
    *,
    owner_qq: str,
    admin_ids: list[str],
    group_ids: list[str],
    mention_keywords: list[str],
    provider_id: str,
    kb_name: str,
    enable_proactive: bool,
    dry: bool,
) -> None:
    plugin_data = data_dir / "plugin_data"
    pg = plugin_data / "astrbot_plugin_persona_growth"
    sm = plugin_data / "astrbot_plugin_self_memory"
    ip = plugin_data / "astrbot_plugin_interest_profile"
    tp = plugin_data / "astrbot_plugin_topic_proficiency"
    gp = plugin_data / "astrbot_plugin_group_proactive"

    for d in (pg, sm, ip, tp, gp):
        if not dry:
            d.mkdir(parents=True, exist_ok=True)

    diary = pg / "diary.json"
    key_facts = sm / "key_facts.json"
    memories = sm / "memories.json"
    prefs = ip / "prefs.json"
    proficiency = tp / "proficiency.json"

    for name in selected:
        cfg_path = plugin_dir / name / "config.json"
        if not cfg_path.exists():
            continue
        cfg = load_json(cfg_path)
        changed = False

        def put(key: str, value) -> None:
            nonlocal changed
            if cfg.get(key) != value:
                cfg[key] = value
                changed = True

        if name == "astrbot_plugin_group_proactive":
            if owner_qq or admin_ids:
                put("admin_id", owner_qq or admin_ids[0])
            if group_ids:
                put("group_ids", group_ids)
            if mention_keywords:
                put("mention_keywords", mention_keywords)
            put("persona_diary_path", str(diary))
            put("diary_file", str(diary))
            put("interest_prefs_path", str(prefs))
            put("interest_proficiency_path", str(proficiency))
            if kb_name:
                put("interest_kb_name", kb_name)
                put("knowledge_backend", "auto")
            put("dry_run", not enable_proactive)
        elif name == "astrbot_plugin_persona_growth":
            put("data_dir", str(pg))
            put("key_facts_path", str(key_facts))
            put("memory_file_path", str(memories))
            if provider_id:
                put("provider_id", provider_id)
            if kb_name:
                put("knowledge_kb_name", kb_name)
                put("knowledge_backend", "auto")
            put("mood_mention_keywords", mention_keywords)
        elif name == "astrbot_plugin_self_memory":
            put("data_dir", str(sm))
            put("diary_path", str(diary))
            put("key_facts_path", str(key_facts))
        elif name == "astrbot_plugin_interest_profile":
            put("diary_path", str(diary))
            put("proficiency_path", str(proficiency))
            if provider_id:
                put("provider_id", provider_id)
            put("dry_run", not enable_proactive)
        elif name == "astrbot_plugin_topic_proficiency":
            put("corpus_dir", str(pg / "surf_corpus"))
            put("cards_dir", str(tp / "cards"))
            put("data_dir", str(tp))
            put("prefs_path", str(prefs))
            put("knowledge_path", str(pg / "knowledge.jsonl"))
            if kb_name:
                put("kb_name", kb_name)
                put("knowledge_backend", "auto")

        if changed:
            print(f"[配置] {name}/config.json")
            if not dry:
                save_json(cfg_path, cfg)


def compile_all(plugin_dir: Path, selected: list[str]) -> bool:
    ok = True
    for name in selected:
        path = plugin_dir / name / "main.py"
        if not path.exists():
            continue
        try:
            py_compile.compile(str(path), doraise=True)
            print(f"[语法] {name} OK")
        except py_compile.PyCompileError as exc:
            ok = False
            print(f"[语法] {name} 失败：{exc}")
    for cache in plugin_dir.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return ok


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="安装 AstrBot 插件包")
    ap.add_argument("--astrbot-data", default=str(default_data_dir()),
                    help="AstrBot 数据目录（含 plugins/、data_v4.db），默认 ~/.astrbot/data")
    ap.add_argument("--owner-qq", default="", help="机器人主人 / 管理员的 QQ 号")
    ap.add_argument("--admin-ids", default="", help="额外管理员 QQ，逗号分隔")
    ap.add_argument("--group-ids", default="", help="允许主动聊天的群号，逗号分隔；留空=不主动进群")
    ap.add_argument("--mention-keywords", default="", help="机器人名字/昵称，逗号分隔；默认用 --bot-name")
    ap.add_argument("--bot-name", default="糯糯", help="机器人名字（会替换插件里的默认名），默认 糯糯")
    ap.add_argument("--provider-id", default="", help="写日记/打分用的模型 ID；留空=用默认模型")
    ap.add_argument("--kb-name", default="", help="AstrBot 知识库名称；留空=不使用知识库")
    ap.add_argument("--enable-proactive", action="store_true",
                    help="安装完立即开启群聊主动聊天（默认 dry_run，不主动发言）")
    ap.add_argument("--plugins", default="", help="只装这些插件，逗号分隔")
    ap.add_argument("--skip", default="", help="跳过这些插件，逗号分隔")
    ap.add_argument("--reconfigure", action="store_true", help="只重写 config.json，不复制插件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的插件目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做什么")
    ap.add_argument("--no-compile", action="store_true", help="跳过语法检查")
    args = ap.parse_args()

    data_dir = Path(args.astrbot_data).expanduser().resolve()
    plugin_dir = data_dir / "plugins"

    selected = list(ALL_PLUGINS)
    if args.plugins:
        wanted = set(parse_list(args.plugins))
        selected = [p for p in selected if p in wanted]
    skip = set(parse_list(args.skip))
    selected = [p for p in selected if p not in skip]
    if not selected:
        print("没有要安装的插件。")
        return 1

    owner_qq = str(args.owner_qq or "").strip()
    admin_ids = parse_list(args.admin_ids)
    group_ids = parse_list(args.group_ids)
    bot_name = str(args.bot_name or "").strip() or "糯糯"
    keywords = parse_list(args.mention_keywords)
    if not keywords:
        keywords = list(NAME_VARIANTS) if bot_name == "糯糯" else [bot_name]

    print("=" * 60)
    print("AstrBot 数据目录：", data_dir)
    print("插件目录：        ", plugin_dir)
    print("要安装的插件：    ", ", ".join(selected))
    print("机器人名字：      ", bot_name)
    print("主人 QQ：         ", owner_qq or "（未填，稍后请在 WebUI 设置 admins_id）")
    print("群号：            ", ", ".join(group_ids) or "（空，不会主动进群）")
    print("主动聊天：        ", "安装后开启" if args.enable_proactive else "dry_run（只记录不发言）")
    print("=" * 60)

    if not data_dir.exists():
        print(f"\n[错误] 找不到数据目录 {data_dir}")
        print("请确认 AstrBot 已经至少启动过一次，或手动指定 --astrbot-data。")
        return 1
    if not plugin_dir.exists():
        print(f"[提示] {plugin_dir} 不存在，将新建。")
        if not args.dry_run:
            plugin_dir.mkdir(parents=True, exist_ok=True)

    if not args.reconfigure:
        copy_plugins(selected, plugin_dir, args.force, args.dry_run)
        rename_names(plugin_dir, selected, bot_name, args.dry_run)

    build_configs(
        selected,
        plugin_dir,
        data_dir,
        owner_qq=owner_qq,
        admin_ids=admin_ids,
        group_ids=group_ids,
        mention_keywords=keywords,
        provider_id=str(args.provider_id or "").strip(),
        kb_name=str(args.kb_name or "").strip(),
        enable_proactive=args.enable_proactive,
        dry=args.dry_run,
    )

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。")
        return 0

    if not args.no_compile:
        if not compile_all(plugin_dir, selected):
            print("\n[警告] 有插件语法检查失败，请把上面的报错发给帮你部署的 AI。")
            return 2

    print("\n完成。下一步：")
    print("1. 打开 AstrBot WebUI → 配置，把主人 QQ 填进 admins_id（如果安装时没填）。")
    print("2. 确认消息平台是 aiocqhttp / NapCat，并且已经连上。")
    print("3. 确认默认模型可用；如果有多个模型，把 --provider-id 填成 WebUI 里显示的 ID。")
    print("4. 在 WebUI 插件页重载插件（或重启 AstrBot）。")
    print("5. 先按 dry_run 观察日志，确认没问题后再开主动聊天。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
