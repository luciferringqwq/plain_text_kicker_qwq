"""plain_text_kicker 插件入口。

在 LLM 输出非工具调用的纯文本时直接发送为回复，
模型同时输出回复文本与 action-do_nothing 时同样发送该文本，
模型输出正文 + 其它工具调用（如 kfc_reply 收到空 content）时也补发正文，
并注入虚假 action-do_nothing 阻止 chatter 重试。
"""

from __future__ import annotations

from src.core.components import BasePlugin, register_plugin

from .config import PlainTextKickerConfig
from .handler import PlainReplyHandler
from .service import PlainReplyService


@register_plugin
class PlainTextKickerPlugin(BasePlugin):
    """踹飞纯文本插件。"""

    plugin_name: str = "plain_text_kicker"
    plugin_description: str = (
        "截获模型非工具调用的纯文本输出，直接发送为聊天回复；"
        "模型写了回复却又调用 do_nothing，或正文与工具调用并存"
        "（如 kfc_reply 空 content）时补发该文本，"
        "注入虚假 action-do_nothing 阻止 chatter 重试循环"
    )
    plugin_version: str = "1.1.1"

    configs: list[type] = [PlainTextKickerConfig]

    def get_components(self) -> list[type]:
        """返回本插件提供的组件类。"""
        return [PlainReplyService, PlainReplyHandler]
