from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

# QQ 空间权限：1 公开，4 好友可见，64 仅自己可见。
UGC_RIGHT = {
    "public": 1,
    "公开": 1,
    "所有人": 1,
    "friends": 4,
    "好友": 4,
    "friends_only": 4,
    "private": 64,
    "仅自己": 64,
    "自己": 64,
}

_SPLIT_RE = re.compile(r"[\n,，;；]+")


class Main(star.Star):
    """管理机器人自己的 QQ 资料与 QQ 空间说说。"""

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)

    # ---------- 基础 ----------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        return bool(checker()) if callable(checker) else False

    def _call_action(self, event: AstrMessageEvent):
        platform = self.context.get_platform_inst(event.get_platform_id())
        if platform is None:
            return None, "error: 未找到当前平台实例。"
        client = platform.get_client()
        call_action = getattr(client, "call_action", None)
        if not callable(call_action):
            return None, "error: 当前平台不支持调用 OneBot 接口。"
        return call_action, None

    async def _current_nickname(self, call_action) -> str:
        try:
            info = await call_action("get_login_info")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"获取机器人昵称失败：{exc}")
            return ""
        if isinstance(info, dict):
            info = info.get("data") or info
        if isinstance(info, dict):
            return str(info.get("nickname") or "").strip()
        return ""

    @staticmethod
    def _split_items(raw: str) -> list[str]:
        return [item.strip() for item in _SPLIT_RE.split(str(raw or "")) if item.strip()]

    # ---------- 资料 ----------

    @filter.llm_tool(name="set_qq_profile")
    async def set_qq_profile(
        self, event: AstrMessageEvent, signature: str = "", nickname: str = ""
    ) -> str:
        """修改机器人自己的 QQ 个性签名或昵称。仅管理员可用。只想改签名时 nickname 留空。

        Args:
            signature(string): 新的个性签名；不改签名就留空。
            nickname(string): 新的 QQ 昵称；不改昵称就留空。
        """
        if not self._is_admin(event):
            return "error: 只有管理员可以修改机器人的 QQ 资料。"

        sig = str(signature or "").strip()
        nick = str(nickname or "").strip()
        if not sig and not nick:
            return "error: 签名和昵称至少要提供一个。"
        if len(sig) > 120:
            return "error: 个性签名太长了，请控制在 120 字以内。"
        if len(nick) > 30:
            return "error: 昵称太长了，请控制在 30 字以内。"

        call_action, error = self._call_action(event)
        if error:
            return error

        if not nick:
            nick = await self._current_nickname(call_action)
        if not nick:
            return "error: 没取到机器人当前昵称，请同时提供 nickname 再试一次。"

        payload: dict[str, Any] = {"nickname": nick}
        if sig:
            payload["personal_note"] = sig

        try:
            await call_action("set_qq_profile", **payload)
        except Exception as exc:  # noqa: BLE001
            return f"error: 修改 QQ 资料失败：{exc}"

        changed = []
        if sig:
            changed.append(f"签名改为「{sig}」")
        if nickname:
            changed.append(f"昵称改为「{nick}」")
        return "已修改机器人资料：" + "，".join(changed) + "。"

    # ---------- QQ 空间 ----------

    @filter.llm_tool(name="publish_qzone_moment")
    async def publish_qzone_moment(
        self,
        event: AstrMessageEvent,
        content: str,
        images: str = "",
        visibility: str = "public",
    ) -> str:
        """在机器人自己的 QQ 空间发布一条说说。仅管理员可用；发之前先自己确认内容，不要发敏感信息，写不出来的话直接拒绝。

        Args:
            content(string): 说说正文。
            images(string): 可选，图片地址，多个用逗号或换行分隔；支持 http(s) 链接或本地路径。没有就留空。
            visibility(string): 可选，可见范围：public（公开，默认）、friends（好友可见）、private（仅自己可见）。
        """
        if not self._is_admin(event):
            return "error: 只有管理员可以发说说。"
        body = str(content or "").strip()
        if not body:
            return "error: 说说内容不能为空。"
        if len(body) > 1000:
            return "error: 说说正文太长了，请控制在 1000 字以内。"

        image_list = self._split_items(images)
        right_key = str(visibility or "public").strip().lower()
        ugc_right = UGC_RIGHT.get(right_key)
        if ugc_right is None:
            try:
                ugc_right = int(right_key)
            except (TypeError, ValueError):
                return "error: visibility 只能是 public、friends 或 private。"

        call_action, error = self._call_action(event)
        if error:
            return error

        try:
            result = await call_action(
                "send_qzone_msg",
                content=body,
                images=image_list,
                ugc_right=ugc_right,
            )
        except Exception as exc:  # noqa: BLE001
            return f"error: 发说说失败：{exc}"

        if isinstance(result, dict):
            result = result.get("data") or result
        tid = ""
        if isinstance(result, dict):
            tid = str(result.get("tid") or "").strip()

        scope = {1: "公开", 4: "好友可见", 64: "仅自己可见"}.get(ugc_right, str(ugc_right))
        suffix = f"，tid={tid}" if tid else ""
        return f"已发布说说（{scope}{suffix}）：{body[:60]}"

    @filter.llm_tool(name="delete_qzone_moment")
    async def delete_qzone_moment(self, event: AstrMessageEvent, tid: str) -> str:
        """删除机器人 QQ 空间里的一条说说。仅管理员可用。需要 tid（发说说时会返回）。

        Args:
            tid(string): 说说的 tid，发布时会返回。
        """
        if not self._is_admin(event):
            return "error: 只有管理员可以删除说说。"
        target = str(tid or "").strip()
        if not target:
            return "error: tid 不能为空。"

        call_action, error = self._call_action(event)
        if error:
            return error
        try:
            await call_action("delete_qzone_msg", tid=target)
        except Exception as exc:  # noqa: BLE001
            return f"error: 删除说说失败：{exc}"
        return f"已删除说说（tid={target}）。"
