"""plain_text_kicker_qwq 插件：踹飞纯文本。

在 AFTER_LLM_REQUEST 事件中截获模型非工具调用的纯文本输出，
直接发送为聊天回复，并注入虚假 action-do_nothing 工具调用
阻止 chatter（如 KFC）因 call_list 为空而触发纯文本重试循环。

除纯文本外，还覆盖「文本 + do_nothing」与「文本 + 其它工具调用」
（典型为 kfc_reply 收到空 content）两类正文被静默丢弃的响应。
"""
