# NOTICE

本仓库包含两部分内容，许可证适用范围不同：

## 1. 插件与工具（原创部分）

`plugins/`、`tools/`、`docs/`、`README.md`、`部署文档.md` 为本项目原创代码与文档，按仓库根目录的 `LICENSE`（GNU AGPL-3.0）发布。

## 2. AstrBot 本体补丁（`core-patches/`）

`core-patches/` 下是对 **AstrBot 4.29.3** 本体文件的修改版整文件，来源于：

- 项目：AstrBot
- 仓库：https://github.com/AstrBotDevs/AstrBot
- 版本：4.29.3
- 许可证：GNU Affero General Public License v3.0（AGPL-3.0）

这些文件不是 AstrBot 官方发布版本，包含本地修改。修改点、文件清单、SHA256 和适用版本见 `core-patches/使用说明.md` 与 `core-patches/manifest.json`。

按 AGPL-3.0 的要求，本仓库同样以 AGPL-3.0 发布，并保留上游许可证与出处说明。若你分发本仓库或基于它提供网络服务，请遵守 AGPL-3.0 的相应义务。

## 3. 不包含的内容

本仓库不包含任何人的日记、长期记忆、聊天记录、群号、QQ 号、会话数据库、日志、备份或 AstrBot 全局配置。发布前的自动检查见 `tools/verify.py`。
