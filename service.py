"""纯文本回复服务。

为其他 chatter 提供显式的纯文本发送接口，
让 chatter 绕过工具调用流程直接发送纯文本。
"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.core.components.base.service import BaseService

logger = get_logger("plain_text_kicker")


class PlainReplyService(BaseService):
    """纯文本回复服务。

    供其他 chatter 通过 ServiceManager 获取并调用：
    当 chatter 发现 LLM 输出为纯文本（非工具调用）时，
    调用 try_send() 将文本直接发送出去。

    Examples:
        >>> svc = service_manager.get_service("plain_text_kicker:text_reply")
        >>> await svc.try_send("你好呀", stream_id="qq_123")
    """
    service_description = "直接发送纯文本回复的服务"
    service_name = "text_reply"

    name: str = "text_reply"
    description: str = "直接发送纯文本回复的服务"
    version: str = "1.0.1"

    async def try_send(
        self,
        content: str,
        stream_id: str,
        *,
        max_length: int = 4096,
    ) -> bool:
        """尝试发送纯文本。

        Args:
            content: 待发送的文本内容
            stream_id: 聊天流 ID
            max_length: 最大发送长度，超过则截断

        Returns:
            bool: 发送是否成功
        """
        if not content or not content.strip() or not stream_id:
            return False

        text = content.strip()
        if len(text) > max_length:
            text = text[:max_length]
            logger.debug(f"[plain_text_kicker.text_reply] 截断到 {max_length} 字符")

        try:
            success = await send_text(content=text, stream_id=stream_id)
            if success:
                logger.debug(
                    f"[plain_text_kicker.text_reply] 已发送 "
                    f"stream={stream_id[:8]} len={len(text)}"
                )
            else:
                logger.warning(
                    f"[plain_text_kicker.text_reply] 发送失败 "
                    f"stream={stream_id[:8]}"
                )
            return success
        except Exception:
            logger.exception(
                f"[plain_text_kicker.text_reply] 发送异常 "
                f"stream={stream_id[:8]}"
            )
            return False

    async def send_or_raise(
        self,
        content: str,
        stream_id: str,
        *,
        max_length: int = 4096,
    ) -> None:
        """发送纯文本，失败时抛出异常。

        Args:
            content: 待发送的文本内容
            stream_id: 聊天流 ID
            max_length: 最大发送长度

        Raises:
            RuntimeError: 发送失败时抛出
        """
        ok = await self.try_send(content, stream_id, max_length=max_length)
        if not ok:
            raise RuntimeError(
                f"plain_text_kicker 发送失败 stream={stream_id[:8]}"
            )
