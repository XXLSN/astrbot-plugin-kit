# AstrBot「养成系」插件包

这套插件让 AstrBot 里的 QQ 机器人具备一整套「自己长大」的行为：写日记、刷 B 站学说话、记长期记忆、在群里判断该不该接话、主动找话题、控制回复节奏、统计花费。

原作者的机器人叫「糯糯」，所以代码里的默认名字、提示词、示例都是「糯糯」。你可以在安装时用 `--bot-name` 换成自己的名字。

---

## 一、包含的插件（10 个）

| 插件 | 作用 | 是否必需 |
|---|---|---|
| `astrbot_plugin_persona_growth` | 人格成长：写日记、刷 B 站、学说话/学梗、抽取关于主人的事实 | 成长体系核心 |
| `astrbot_plugin_self_memory` | 自主记忆：自己记/忘长期记忆、清上下文、搜历史 | 强烈建议 |
| `astrbot_plugin_interest_profile` | 兴趣偏好：从娱乐日记里提炼喜欢/讨厌，供群聊判断 | 可选 |
| `astrbot_plugin_topic_proficiency` | 话题熟练度：按看过的素材算「这个话题她熟不熟」 | 可选 |
| `astrbot_plugin_group_proactive` | 群聊主动聊天：上线补读、兴趣接话、被叫到才回、私聊主动 | 可选（建议先 dry-run） |
| `astrbot_plugin_reply_pacing` | 回复节奏：连续回复之间留几秒，避免刷屏 | 建议 |
| `astrbot_plugin_context_slim` | 上下文瘦身：清旧图、思考链、超长工具结果，省 token | 建议 |
| `astrbot_plugin_usage_cost` | 用量花费：按天统计 token 并估算花费 | 可选 |
| `astrbot_plugin_cross_group` | 跨群助手：管理员私聊里读群历史、往指定群发消息 | 可选 |
| `astrbot_plugin_qq_profile` | QQ 资料：改签名/昵称、发/删空间说说（需要 NapCat） | 可选 |

依赖关系见 `docs/依赖关系.md`。不知道怎么选、哪些插件搭配效果更好，看 [`docs/插件简介与搭配推荐.md`](docs/插件简介与搭配推荐.md)。

**知识检索默认走「非向量数据库路线」**：只读 `persona_growth` 生成的 `knowledge.jsonl`，不需要 embedding provider、Ollama 或 WebUI 知识库。要改回向量库，见 `部署文档.md` 第 6.3.1 节。

---

## 二、单独下载某个插件

每次发版会把 10 个插件分别打成独立 zip，放在 **Releases** 里。只想装一两个插件时，直接下对应的包：

| 插件 | 单独下载 |
|---|---|
| 人格成长 `persona_growth` | [astrbot_plugin_persona_growth.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_persona_growth.zip) |
| 自主记忆 `self_memory` | [astrbot_plugin_self_memory.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_self_memory.zip) |
| 兴趣偏好 `interest_profile` | [astrbot_plugin_interest_profile.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_interest_profile.zip) |
| 话题熟练度 `topic_proficiency` | [astrbot_plugin_topic_proficiency.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_topic_proficiency.zip) |
| 群聊主动 `group_proactive` | [astrbot_plugin_group_proactive.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_group_proactive.zip) |
| 回复节奏 `reply_pacing` | [astrbot_plugin_reply_pacing.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_reply_pacing.zip) |
| 上下文瘦身 `context_slim` | [astrbot_plugin_context_slim.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_context_slim.zip) |
| 用量花费 `usage_cost` | [astrbot_plugin_usage_cost.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_usage_cost.zip) |
| 跨群助手 `cross_group` | [astrbot_plugin_cross_group.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_cross_group.zip) |
| QQ 资料 `qq_profile` | [astrbot_plugin_qq_profile.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot_plugin_qq_profile.zip) |

整包（10 个插件 + 工具 + 文档 + 本体补丁）：

- [astrbot-plugin-kit.zip](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/astrbot-plugin-kit.zip)
- [SHA256SUMS.txt](https://github.com/XXLSN/astrbot-plugin-kit/releases/latest/download/SHA256SUMS.txt)

下载单个插件后，解压到 AstrBot 数据目录的 `plugins/` 下，例如：

```text
~/.astrbot/data/plugins/astrbot_plugin_persona_growth/
```

然后重载插件或重启 AstrBot。`persona_growth` 和 `self_memory` 建议一起装；其余插件可以按需组合，依赖关系见 `docs/依赖关系.md`。

> 上面的链接指向 `releases/latest`，第一次发版之后才会生效。

---

## 三、不包含的内容（故意）

- 原作者的 `diary.json`、`memories.json`、`prefs.json`、`state.json`、`knowledge.jsonl`、`surf_corpus/`、`msgs/`、`ledger.json`、`decisions.jsonl`
- 原作者的 QQ 号、群号、管理员 ID、机器人昵称
- 原作者的 `data_v4.db`、日志、备份
- 插件目录里的 `cmd_config.json`（它包含 AstrBot 全局配置和密码哈希）
- 第三方插件 `astrbot_plugin_qzone`（作者 Zhalslar，不是这套代码）

---

## 四、前置条件

1. **AstrBot**：本包在 **v4.29.3 桌面版**上开发和验证。建议 v4.29.x 或更新。
2. **消息平台**：`aiocqhttp`（NapCat / OneBot v11）。所有插件都只声明支持 `aiocqhttp`。
3. **一个可用的大模型**：在 WebUI「服务提供商」里配好，并设为默认。
4. **管理员 QQ**：填进 AstrBot 全局配置的 `admins_id`。
5. **Python 3.10+**：安装器只用标准库，系统 Python 或 AstrBot 自带的 Python 都能跑。

不需要 Docker、不需要额外 pip 包。`persona_growth` 用到 `aiohttp`，AstrBot 自带。

---

## 五、三步安装

先关掉 AstrBot（或至少不要在安装时重载插件），然后：

```bash
# 1. 进入解压后的目录
cd astrbot-plugin-kit

# 2. 安装（把 QQ 和群号换成你自己的）
python tools/install.py --owner-qq <你的QQ> --group-ids <群号1,群号2>

# 3. 自检
python tools/verify.py
```

安装器会自动找到 `~/.astrbot/data`（Windows 是 `C:\Users\<你>\.astrbot\data`）。如果 AstrBot 装在别处，加 `--astrbot-data "<数据目录>"`。

安装完成后：

1. 打开 AstrBot WebUI → 配置 → 把 `<你的QQ>` 填进 `admins_id`。
2. 确认消息平台已连接、默认模型可用。
3. 在插件页重载，或重启 AstrBot。
4. **先保持 `dry_run: true` 观察一两天**，确认日志里的判断没问题，再开启主动聊天。

详细的逐步部署（包括每个 WebUI 选项、验收命令、报错处理）见 **`部署文档.md`**。这份文档可以直接整篇发给 AI，让它照着做。

---

## 六、AstrBot 本体补丁（可选）

包里额外带了 9 个对 AstrBot 本体（`backend/app/astrbot/...`）的修改，放在 `core-patches/`。**插件不装这些也能跑**，装了的收益是：

- 合并转发展开（含图片、文件正文）
- QQ 表情/超级表情显示成可读文本
- 回旧消息可以带引用
- 群历史更干净（不堆 `[图片]` 占位、时间正确）
- 定时任务只有管理员能操作
- 主动任务不把旁白写回会话

**适用版本：AstrBot 4.29.3。** 这是整文件替换，不是 diff 补丁，跨版本直接覆盖有风险。

```bash
# 先预览（不会写文件）
python tools/apply_core_patches.py --astrbot-home "<AstrBot安装目录>" --dry-run

# 停掉 AstrBot 后正式应用（自动备份原文件）
python tools/apply_core_patches.py --astrbot-home "<AstrBot安装目录>"

# 出问题就恢复
python tools/apply_core_patches.py --astrbot-home "<AstrBot安装目录>" --restore "<备份目录>"
```

详细清单、验收步骤、版本不一致怎么处理，见 **`core-patches/使用说明.md`**。

建议顺序：**先装插件跑通，再装本体补丁逐项验收**，出问题好定位。

---

## 七、目录结构

```
astrbot-plugin-kit/
├── README.md                  ← 本文件
├── 部署文档.md                ← 给 AI / 人看的完整部署步骤
├── plugins/                   ← 10 个插件（只有 main.py / metadata.yaml / 脱敏 config.json）
│   ├── astrbot_plugin_persona_growth/
│   ├── astrbot_plugin_self_memory/
│   └── ...
├── core-patches/              ← AstrBot 本体补丁（可选，适用 4.29.3）
│   ├── 使用说明.md
│   ├── manifest.json          ← 文件清单 + SHA256 + 来源版本
│   └── backend/app/astrbot/... ← 9 个本体文件
├── tools/
│   ├── install.py             ← 安装器：复制 + 生成配置 + 语法检查
│   ├── verify.py              ← 发布前自检：查个人数据/绝对路径/编译
│   └── apply_core_patches.py  ← 应用/恢复本体补丁（带版本检查与备份）
└── docs/
    ├── 插件说明.md            ← 每个插件做什么、有哪些工具/命令、写哪些文件
    ├── 配置说明.md            ← 关键配置项、默认值、推荐值
    ├── 依赖关系.md            ← 谁读谁的文件、最小可用组合
    └── 已知限制与安全.md      ← 平台限制、风控、隐私、回滚
```

---

## 八、默认安全策略

安装器生成的配置默认是「保守模式」：

- `group_proactive.dry_run = true`：只记录「本来会不会回」，不真的发言。
- `group_proactive.group_ids = []`：不主动进任何群。
- `group_proactive.private_friends_enabled = false`：不主动私聊普通好友。
- `interest_profile.dry_run = true`：只分析，不写偏好文件。
- `group_proactive.admin_id`：只有安装时填的 QQ 能触发管理员工具。

确认没问题后，用 `--enable-proactive` 重新跑一次安装器（或手动把两个 `dry_run` 改成 `false`），再填 `group_ids`。

---

## 九、升级 / 卸载

- **升级**：用新包覆盖 `plugins/` 下对应目录，保留 `config.json` 和 `plugin_data/`。安装器默认不覆盖已存在目录；要覆盖加 `--force`（会删掉该插件目录，`plugin_data` 不受影响）。
- **卸载**：删除 `plugins/astrbot_plugin_*` 对应目录；`plugin_data/` 里的数据想留就留，想清就清。
- **回滚**：安装前备份 `plugins/` 和 `data_v4.db`，出问题直接还原。

---

## 十、文档索引

| 想知道什么 | 看哪里 |
|---|---|
| 怎么一步步装、怎么验收 | `部署文档.md` |
| 每个插件具体做什么 | `docs/插件说明.md` |
| 插件怎么用、哪些搭配更好 | `docs/插件简介与搭配推荐.md` |
| 某个配置项什么意思 | `docs/配置说明.md` |
| 哪些插件可以不要 | `docs/依赖关系.md` |
| 本体补丁是什么、怎么装、怎么回滚 | `core-patches/使用说明.md` |
| 为什么她不说话 / 报错 / 风控 | `docs/已知限制与安全.md` |

---

## 十一、许可证与出处

本包以 **GNU AGPL-3.0** 发布，完整文本见 `LICENSE`。

- 插件、工具、文档：本项目原创。
- `core-patches/`：基于 **AstrBot 4.29.3**（https://github.com/AstrBotDevs/AstrBot ，AGPL-3.0）修改的整文件，不是官方版本。修改点见 `core-patches/使用说明.md`，文件哈希见 `core-patches/manifest.json`。
- 上游出处与许可证说明见 `NOTICE.md`。

如果你只想使用插件、不想承担本体补丁的许可证义务，可以只取 `plugins/`、`tools/`、`docs/`，不安装也不分发 `core-patches/`。
