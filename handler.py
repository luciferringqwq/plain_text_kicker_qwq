"""AFTER_LLM_REQUEST 事件处理器：截获纯文本回复并直接发送。

在 LLM 响应返回给 chatter 之前，检测非 JSON 工具调用的纯文本输出，
直接通过 send_api 发送，并注入假的 action-do_nothing 工具调用以阻止
chatter（如 KFC）因 call_list 为空而触发纯文本重试循环。

共覆盖三类「正文会被静默丢弃」的响应：

1. **纯文本**：``message`` 有正文、``tool_calls`` 为空；
2. **文本 + do_nothing**：模型把回复写进 message，却又调用 ``do_nothing``
   表示沉默，chatter 只执行工具调用，正文被丢弃；
3. **文本 + 其它工具调用**：模型同时给出正文与真实工具调用（例如
   ``kfc_reply`` 收到空 ``content``，执行层判「content 解析后为空，
   视为工具失败」），chatter 只认工具调用，正文同样被丢弃。

前两类发送后用假 ``action-do_nothing`` 整体替换 ``tool_calls``，阻止
chatter 重试；第 3 类只补发正文，``tool_calls`` 原样交回 chatter 执行。
第 3 类可用 ``intercept_tool_calls_with_text`` 关闭。

第 3 类带**去重保护**：若工具调用本身就是「把文本发给用户」的回复类动作
（名称命中 ``TEXT_SENDING_CALL_PATTERNS``）且已自带非空文本，说明这条消息
会由该工具发出去，此时不再补发，避免用户收到双份。只消费文本、不发给
用户的动作（如 ``kfc_memo`` 也把正文叫 ``content``）不在其列，不会误伤。

stream_id 通过 meta_data 或 chatter_manager 反向查找获取，
兼容未在 LLMRequest.meta_data 中设置 stream_id 的 chatter（如 kokoro_flow_chatter）。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections import OrderedDict
from typing import Any, Iterator

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventType
from src.core.managers.chatter_manager import get_chatter_manager
from src.kernel.event import EventDecision

logger = get_logger("plain_text_kicker")

TEXT_ARG_KEYS: tuple[str, ...] = (
    "content",
    "text",
    "message",
    "segments",
    "msg",
    "reply",
    "reply_text",
    "texts",
)
"""被视为「文本载荷」的工具参数名。

仅用于判断回复类调用是否已自带文本；命中的参数为空即视为没给出文本。
"""

TEXT_SENDING_CALL_PATTERNS: tuple[str, ...] = (
    "*kfc_reply*",
    "*send_text*",
    "*send_message*",
    "*reply*",
)
"""被视为「会把文本发给用户」的回复类调用名模式（glob）。

只有命中这些模式的调用才参与去重判断。像 ``kfc_memo`` 这种只是把文本
存起来的动作，即便参数同样叫 ``content``，也不会让插件误以为消息已发出。
"""

MODE_PLAIN_TEXT = "plain_text"
"""纯文本模式：message 有正文、tool_calls 为空。"""

MODE_SILENCE_WITH_TEXT = "silence_with_text"
"""文本 + do_nothing 模式：沉默决策里夹带了正文。"""

MODE_TOOL_CALLS_WITH_TEXT = "tool_calls_with_text"
"""文本 + 其它工具调用模式：正文与真实工具调用并存。"""


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

        检测以下三类响应并直接发送其中的文本：

        1. 纯文本 + 无 tool_calls；
        2. 文本 + 仅含 ``action-do_nothing`` 的工具调用（沉默决策）；
        3. 文本 + 其它工具调用。

        前两类发送后把 ``tool_calls`` 整体替换为假 ``action-do_nothing``，
        第 3 类只补发正文、``tool_calls`` 原样保留。
        stream_id 来源优先级：meta_data → chatter_manager 反向查找。
        """
        # ── AFTER_LLM_REQUEST：拦截纯文本 ──
        config = self._get_config()
        if config is None or not config.enabled:
            return EventDecision.SUCCESS, params

        # 1. 提取关键字段
        message = params.get("message")
        tool_calls = list(params.get("tool_calls") or [])
        request_name = str(params.get("request_name") or "")
        meta_data = params.get("meta_data") or {}

        # stream_id 优先级：meta_data > chatter_manager
        stream_id = str(meta_data.get("stream_id") or "")
        if not stream_id:
            stream_id = self._lookup_stream_id_by_request(request_name)

        silence_only = self._is_silence_only(tool_calls)
        reply_call_has_text = self._has_reply_call_with_text(tool_calls)
        mode = self._resolve_mode(
            message=message,
            tool_calls=tool_calls,
            silence_only=silence_only,
            reply_call_has_text=reply_call_has_text,
            request_name=request_name,
            stream_id=stream_id,
            config=config,
        )

        if config.debug_log:
            logger.info(
                f"[plain_text_kicker] AFTER_LLM_REQUEST "
                f"req={request_name} "
                f"msg={bool(message)} "
                f"tc={len(tool_calls)} "
                f"mode={mode or 'SKIP'} "
                f"reply_call_has_text={reply_call_has_text} "
                f"sid={'...'+stream_id[-8:] if stream_id else 'NONE'}"
            )
            if mode is None and reply_call_has_text:
                logger.debug(
                    "[plain_text_kicker] 回复类调用已自带文本，交由工具发送，跳过补发"
                )

        # 2. 过滤
        if mode is None:
            return EventDecision.SUCCESS, params

        text = str(message).strip()

        # 3. 去重（命中时不再发送，但仍按模式收口工具调用）
        if config.enable_dedup and self._is_duplicate(stream_id, text):
            if config.debug_log:
                logger.debug(
                    f"[plain_text_kicker] 去重跳过 stream={stream_id[:8]} "
                    f"text_len={len(text)}"
                )
            params["tool_calls"] = self._rewrite_tool_calls(mode, tool_calls)
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
            if mode == MODE_SILENCE_WITH_TEXT:
                logger.info(
                    f"[plain_text_kicker] 文本与 do_nothing 同时出现，"
                    f"已按回复发送 stream={stream_id[:8]} len={len(text)}"
                )
            elif mode == MODE_TOOL_CALLS_WITH_TEXT:
                logger.info(
                    f"[plain_text_kicker] 文本与工具调用并存，"
                    f"已按回复发送 stream={stream_id[:8]} len={len(text)} "
                    f"调用列表={[self._call_name(c) for c in tool_calls]}"
                )
            elif config.debug_log:
                logger.debug(
                    f"[plain_text_kicker] 已发送 stream={stream_id[:8]} "
                    f"len={len(text)} req={request_name}"
                )
            self._mark_sent(stream_id, text, config)
        else:
            logger.warning(f"[plain_text_kicker] 发送失败 stream={stream_id[:8]}")

        # 6. 改写工具调用：前两类注入假 do_nothing 阻止 chatter 重试
        params["tool_calls"] = self._rewrite_tool_calls(mode, tool_calls)

        return EventDecision.SUCCESS, params

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_mode(
        *,
        message: Any,
        tool_calls: list[Any],
        silence_only: bool,
        reply_call_has_text: bool,
        request_name: str,
        stream_id: str,
        config: Any,
    ) -> str | None:
        """判断当前响应属于哪种可拦截模式，不可拦截时返回 ``None``。

        Args:
            message: 模型输出的正文。
            tool_calls: 模型输出的工具调用列表。
            silence_only: 工具调用是否非空且全部为 ``do_nothing``。
            reply_call_has_text: 是否存在已自带非空文本的回复类调用。
            request_name: LLM 请求名。
            stream_id: 聊天流 ID。
            config: 本插件 main 配置节。

        Returns:
            str | None: 命中的模式常量，未命中为 ``None``。
        """
        if not message or not isinstance(message, str) or not message.strip():
            return None
        if not stream_id or not request_name:
            return None
        patterns = config.request_name_patterns or ["*"]
        if not any(fnmatch.fnmatch(request_name, p) for p in patterns):
            return None

        if not tool_calls:
            return MODE_PLAIN_TEXT

        if silence_only:
            if not getattr(config, "intercept_silence_with_text", True):
                return None
            return MODE_SILENCE_WITH_TEXT

        # 正文与真实工具调用并存
        if not getattr(config, "intercept_tool_calls_with_text", True):
            return None
        if reply_call_has_text:
            # 回复类调用自己会把这条消息发出去，补发会变成双份
            return None
        return MODE_TOOL_CALLS_WITH_TEXT

    def _rewrite_tool_calls(self, mode: str, tool_calls: list[Any]) -> list[Any]:
        """按拦截模式改写 ``tool_calls``。

        纯文本与「文本 + do_nothing」两种模式整体替换为假
        ``action-do_nothing``，让 chatter 自然收口而不重试；
        「文本 + 其它工具调用」模式原样保留，交给 chatter 正常执行。

        Args:
            mode: ``_resolve_mode`` 返回的模式常量。
            tool_calls: 原始工具调用列表。

        Returns:
            list[Any]: 改写后的工具调用列表。
        """
        if mode == MODE_TOOL_CALLS_WITH_TEXT:
            return tool_calls
        return self._build_silence_call_list()

    @staticmethod
    def _is_silence_only(tool_calls: Any) -> bool:
        """判断工具调用是否非空且全部为 ``do_nothing``（沉默决策）。

        兼容 ``ToolCall`` 对象与 dict 两种形态，名称兼容
        ``do_nothing`` / ``action-do_nothing`` / ``前缀:action-do_nothing``。
        """
        if not tool_calls:
            return False
        for call in tool_calls:
            name = PlainReplyHandler._call_name(call)
            normalized = name.replace(":", "-").rsplit("-", 1)[-1]
            if normalized != "do_nothing":
                return False
        return True

    @staticmethod
    def _call_name(call: Any) -> str:
        """取工具调用名，兼容 ``ToolCall`` 对象与 dict。"""
        name = getattr(call, "name", None)
        if name is None and isinstance(call, dict):
            name = call.get("name")
        return str(name or "")

    @staticmethod
    def _call_args(call: Any) -> dict[str, Any]:
        """取工具调用参数，兼容 dict / JSON 字符串 / 缺失三种形态。"""
        args = getattr(call, "args", None)
        if args is None and isinstance(call, dict):
            args = call.get("args")
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _iter_text(value: Any) -> Iterator[str]:
        """迭代任意形态参数里的非空文本片段。

        判定口径与 KFC 执行层的 ``parse_content_segments`` 保持一致：
        列表逐项取字符串，字符串再尝试按 JSON 数组二次解析，
        以便 ``'["你好"]'`` 这种被序列化的形状也能正确识别。
        """
        if value is None:
            return
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                yield stripped
                return
            if isinstance(parsed, list):
                for item in parsed:
                    if str(item).strip():
                        yield str(item).strip()
                return
            yield stripped
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                yield from PlainReplyHandler._iter_text(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from PlainReplyHandler._iter_text(item)

    @staticmethod
    def _is_reply_call(name: str) -> bool:
        """判断调用名是否属于「会把文本发给用户」的回复类动作。"""
        return any(
            fnmatch.fnmatch(name, pattern)
            for pattern in TEXT_SENDING_CALL_PATTERNS
        )

    def _call_has_text(self, call: Any) -> bool:
        """判断该调用的文本参数里是否存在非空文本。"""
        args = self._call_args(call)
        for key in TEXT_ARG_KEYS:
            if key not in args:
                continue
            for _ in self._iter_text(args[key]):
                return True
        return False

    def _has_reply_call_with_text(self, tool_calls: list[Any]) -> bool:
        """判断是否存在「已自带非空文本的回复类调用」。

        命中即说明这条消息会由该调用自己发出去，插件不应再补发，
        否则用户会收到双份。只消费文本、不发给用户的动作
        （如 ``kfc_memo`` 的参数同样叫 ``content``）不会被计入。

        Args:
            tool_calls: 工具调用列表。

        Returns:
            bool: 是否命中。
        """
        for call in tool_calls:
            if not self._is_reply_call(self._call_name(call)):
                continue
            if self._call_has_text(call):
                return True
        return False

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
            "name": "action-do-nothing",
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
