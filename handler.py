"""AFTER_LLM_REQUEST 事件处理器：截获纯文本回复并直接发送。

在 LLM 响应返回给 chatter 之前，检测非 JSON 工具调用的纯文本输出，
直接通过 send_api 发送，并注入假的 action-do_nothing 工具调用以阻止
chatter（如 KFC）因 call_list 为空而触发纯文本重试循环。

另外覆盖「回复文本 + action-do_nothing」的组合：部分模型会把回复写进
message，却又调用 do_nothing 表示沉默。此时 chatter 只执行工具调用，
message 会被静默丢弃，因此这里同样把文本发送出去（可由配置关闭）。

stream_id 通过 meta_data 或 chatter_manager 反向查找获取，
兼容未在 LLMRequest.meta_data 中设置 stream_id 的 chatter（如 kokoro_flow_chatter）。
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections import OrderedDict
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventType
from src.core.managers.chatter_manager import get_chatter_manager
from src.kernel.event import EventDecision

logger = get_logger("plain_text_kicker")



class PlainReplyHandler(BaseEventHandler):
    """截获 LLM 的纯文本输出，直接发送为聊天回复。

    通过 chatter_manager 反向查找获取 stream_id。
    """
    handler_description = "截获纯文本 LLM 输出并直接发送，注入假 do_nothing 阻止 chatter 重试"
    handler_name = "plain_text_kicker_handler"

    name: str = "plain_reply_handler"
    description: str = "截获纯文本 LLM 输出并直接发送，注入假 do_nothing 阻止 chatter 重试"

    weight: int = 100  # 高优先级，先于其他处理器执行

    init_subscribe: list[EventType | str] = [
        EventType.AFTER_LLM_REQUEST,
    ]


    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
        self._sent_hashes: dict[str, OrderedDict[str, None]] = {}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 AFTER_LLM_REQUEST 事件。

        检测以下两类响应并直接发送其中的文本：

        1. 纯文本 + 无 tool_calls；
        2. 文本 + 仅含 ``action-do_nothing`` 的工具调用（沉默决策）。

        发送后统一用假 ``action-do_nothing`` 替换 tool_calls，避免 chatter 重试。
        stream_id 来源优先级：meta_data → chatter_manager 反向查找。
        """
        # ── AFTER_LLM_REQUEST：拦截纯文本 ──
        config = self._get_config()
        if config is None or not config.enabled:
            return EventDecision.SUCCESS, params

        # 1. 提取关键字段
        message = params.get("message")
        tool_calls = params.get("tool_calls", [])
        request_name = str(params.get("request_name") or "")
        meta_data = params.get("meta_data") or {}

        # stream_id 优先级：meta_data > chatter_manager
        stream_id = str(meta_data.get("stream_id") or "")
        if not stream_id:
            stream_id = self._lookup_stream_id_by_request(request_name)

        silence_with_text = self._is_silence_only(tool_calls)

        if config.debug_log:
            logger.info(
                f"[plain_text_kicker] AFTER_LLM_REQUEST "
                f"req={request_name} "
                f"msg={bool(message)} "
                f"tc={len(tool_calls) if tool_calls else 0} "
                f"silence_only={silence_with_text} "
                f"sid={'...'+stream_id[-8:] if stream_id else 'NONE'}"
            )

        # 2. 过滤
        if not self._should_intercept(
            message, tool_calls, request_name, stream_id, config
        ):
            return EventDecision.SUCCESS, params

        text = str(message).strip()

        # 3. 去重
        if config.enable_dedup and self._is_duplicate(stream_id, text):
            if config.debug_log:
                logger.debug(
                    f"[plain_text_kicker] 去重跳过 stream={stream_id[:8]} "
                    f"text_len={len(text)}"
                )
            params["tool_calls"] = self._build_silence_call_list()
            return EventDecision.SUCCESS, params

        # 4. 截断
        if len(text) > config.max_text_length:
            text = text[: config.max_text_length]

        # 5. 发送
        try:
            success = await send_text(content=text, stream_id=stream_id)
        except Exception:
            logger.exception(f"[plain_text_kicker] send_text 异常 stream={stream_id[:8]}")
            success = False

        if success:
            if silence_with_text:
                logger.info(
                    f"[plain_text_kicker] 文本与 do_nothing 同时出现，"
                    f"已按回复发送 stream={stream_id[:8]} len={len(text)}"
                )
            elif config.debug_log:
                logger.debug(
                    f"[plain_text_kicker] 已发送 stream={stream_id[:8]} "
                    f"len={len(text)} req={request_name}"
                )
            self._mark_sent(stream_id, text, config)
        else:
            logger.warning(f"[plain_text_kicker] 发送失败 stream={stream_id[:8]}")

        # 6. 注入假 do_nothing 以阻止 chatter 重试（保留 message 以确保上下文连贯）
        params["tool_calls"] = self._build_silence_call_list()

        return EventDecision.SUCCESS, params

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _should_intercept(
        message: Any,
        tool_calls: Any,
        request_name: str,
        stream_id: str,
        config: Any,
    ) -> bool:
        """判断是否应该拦截当前 LLM 响应。

        除「纯文本 + 无工具调用」外，也拦截「文本 + 仅 do_nothing」：
        模型写了回复却又选择沉默时，chatter 只会执行工具调用，
        文本会被丢弃，因此这里补一次发送。
        """
        if not message or not isinstance(message, str) or not message.strip():
            return False
        if tool_calls and len(tool_calls) > 0:
            # 仅在工具调用全部为 do_nothing（且配置允许）时继续拦截，
            # 其余工具调用交给 chatter 正常执行。
            if not PlainReplyHandler._is_silence_only(tool_calls):
                return False
            if not getattr(config, "intercept_silence_with_text", True):
                return False
        if not stream_id:
            return False
        if not request_name:
            return False
        patterns = config.request_name_patterns or ["*"]
        return any(fnmatch.fnmatch(request_name, p) for p in patterns)

    @staticmethod
    def _is_silence_only(tool_calls: Any) -> bool:
        """判断工具调用是否非空且全部为 ``do_nothing``（沉默决策）。

        兼容 ``ToolCall`` 对象与 dict 两种形态，名称兼容
        ``do_nothing`` / ``action-do_nothing`` / ``前缀:action-do_nothing``。
        """
        if not tool_calls:
            return False
        for call in tool_calls:
            name = getattr(call, "name", None)
            if name is None and isinstance(call, dict):
                name = call.get("name")
            normalized = str(name or "").replace(":", "-").rsplit("-", 1)[-1]
            if normalized != "do_nothing":
                return False
        return True

    @staticmethod
    def _hash_content(text: str) -> str:
        """SHA256 前 16 字符作为去重 hash。"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def _is_duplicate(self, stream_id: str, text: str) -> bool:
        """检查是否已发送过相同内容。"""
        window = self._sent_hashes.get(stream_id)
        if window is None:
            return False
        return self._hash_content(text) in window

    def _mark_sent(self, stream_id: str, text: str, config: Any) -> None:
        """记录已发送的 hash（LRU 淘汰）。"""
        if stream_id not in self._sent_hashes:
            self._sent_hashes[stream_id] = OrderedDict()
        window = self._sent_hashes[stream_id]
        h = self._hash_content(text)
        window.pop(h, None)
        window[h] = None
        while len(window) > config.dedup_window_size:
            window.popitem(last=False)

    def _get_config(self) -> Any | None:
        """获取本插件 main 配置节。"""
        plugin = getattr(self, "plugin", None)
        if plugin is None:
            return None
        cfg = getattr(plugin, "config", None)
        if cfg is None:
            return None
        return getattr(cfg, "main", cfg)

    @staticmethod
    def _build_silence_call_list() -> list[dict[str, Any]]:
        """构建一个假的 ``action-do_nothing`` 调用字典列表。

        注入到 params[\"tool_calls\"] 后，框架会将其转换为 ToolCall 对象。
        KFC 执行层会识别 ``action-do_nothing``，将其作为\"选择不回复\"处理，
        从而避免 KFC 因 ``call_list`` 为空而触发纯文本重试循环。

        Returns:
            包含单个 ``action-do_nothing`` 调用字典的列表。
        """
        return [{
            "id": "plain_reply_silence",
            "name": "action-do_nothing",
            "args": {},
        }]

    @staticmethod
    def _lookup_stream_id_by_request(request_name: str) -> str:
        """根据 request_name 从 chatter_manager 反向查找 stream_id。

        对应未在 LLMRequest.meta_data 中设置 stream_id 的 chatter。
        若找到唯一匹配的活跃 chatter，返回其 stream_id；否则返回空字符串。
        """
        if not request_name:
            return ""
        try:
            chatter_manager = get_chatter_manager()
            active = chatter_manager.get_active_chatters()
        except Exception:
            return ""

        matches: list[str] = []
        for sid, chatter in active.items():
            if getattr(chatter, "name", "") == request_name:
                matches.append(sid)

        if len(matches) == 1:
            return matches[0]

        # 多匹配或零匹配：不做猜测，返回空
        return ""
