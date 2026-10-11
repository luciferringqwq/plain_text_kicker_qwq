"""plain_text_kicker 插件：踹飞纯文本。

在 AFTER_LLM_REQUEST 事件中截获模型非工具调用的纯文本输出，
直接发送为聊天回复，并注入虚假 action-do_nothing 工具调用
阻止 chatter（如 KFC）因 call_list 为空而触发纯文本重试循环。
"""
