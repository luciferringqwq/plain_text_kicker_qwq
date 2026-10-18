"""plain_text_kicker_qwq 插件配置。

配置文件默认路径：config/plugins/plain_text_kicker_qwq/config.toml
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class PlainTextKickerConfig(BaseConfig):
    """踹飞纯文本插件配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = (
        "踹飞纯文本插件：截获模型非工具调用输出并直接发送"
    )

    @config_section("main")
    class MainSection(SectionBase):
        """纯文本回复主配置节。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 AFTER_LLM_REQUEST 事件拦截",
        )

        request_name_patterns: list[str] = Field(
            default=["*"],
            description=(
                "请求名匹配模式（glob），仅拦截匹配的 LLM 请求。"
                "默认 `*` 匹配所有。已有 stream_id 天然过滤非聊天请求。"
            ),
        )

        intercept_silence_with_text: bool = Field(
            default=True,
            description=(
                "模型同时输出回复文本与 action-do_nothing（沉默决策）时，"
                "是否仍然把该文本作为回复发送。关闭后此类响应按原有沉默逻辑处理"
            ),
        )

        intercept_tool_calls_with_text: bool = Field(
            default=True,
            description=(
                "模型同时输出回复文本与其它工具调用时，是否把该文本作为回复"
                "补发出去（方案 B）。典型场景是 kfc_reply 收到空 content"
                "（执行层判为工具失败），正文既不被执行也发不出去。"
                "若回复类调用已自带非空文本，说明消息会由该工具发出，"
                "插件不重复补发。关闭后此类响应完全交回 chatter 处理"
            ),
        )

        max_text_length: int = Field(
            default=4096,
            description="单次发送文本的最大字符数，超过则截断",
        )

        enable_dedup: bool = Field(
            default=True,
            description="是否启用去重（同一 stream + 相同内容只发一次）",
        )

        dedup_window_size: int = Field(
            default=50,
            description="去重窗口大小（每个 stream 保留最近 N 条 hash）",
        )

        debug_log: bool = Field(
            default=False,
            description="是否输出调试日志",
        )

    main: MainSection = Field(default_factory=MainSection)

