# plain_text_kicker_qwq — 踹飞纯文本的插件

> **关于插件 ID**：本插件原 ID 为 `plain_text_kicker`（作者 `luciferring`，市场版本 1.0.3）。原 GitHub 账号被封、仓库与发布链失效，现以新账号 `luciferringqwq` 重新上架，插件 ID 改为 `plain_text_kicker_qwq`，代码为此前 1.0.3 版本加上「文本 + do_nothing 被静默丢弃」的修复。
>
> 若你此前安装的是旧 ID `plain_text_kicker`，请先删除 `plugins/plain_text_kicker/` 再安装本插件，避免两个插件同时注册同名事件处理器与 `text_reply` 服务。

在 `AFTER_LLM_REQUEST` 事件中截获模型非工具调用的纯文本输出，直接发送为聊天回复，并注入虚假 `action-do_nothing` 工具调用阻止 chatter 重试循环。

同时覆盖「回复文本 + `action-do_nothing`」的组合：部分模型会把回复写进 message 正文，却又调用 `do_nothing` 表示沉默。chatter 只执行工具调用，message 会被静默丢弃（用户收不到任何回复），因此本插件也把这类文本补发出去。

## 解决的问题

部分 chatter（如 **kokoro_flow_chatter**）在 LLM 返回纯文本（而非 JSON 工具调用）时会进入重试循环：
```
if not response.call_list:         # ← 没有 tool_call？
    inject retry reminder           #    注入"请用 tool_call"提示
    continue                        #    回到循环顶重新请求 LLM
```

`plain_text_kicker` 通过监听 `AFTER_LLM_REQUEST` 事件，在响应返回 chatter 之前直接发送纯文本，并用一个虚假的 `action-do_nothing` 调用替换空的 `call_list`，使 chatter 自然收口而不重试。

另有一类丢消息场景：模型**同时**输出了回复正文和 `action-do_nothing`（`call_list` 非空）。此时 chatter 按工具调用执行沉默，正文被丢弃。本插件把「工具调用全是 `do_nothing`」也视为可拦截的纯文本回复，先把正文发出去，再用虚假 silence 调用替换掉原始 `do_nothing`，语义与正常纯文本兜底一致。

## 双模式

| 模式 | 说明 | 侵入性 |
|------|------|--------|
| **EventHandler** | 自动监听 `AFTER_LLM_REQUEST`，无需 chatter 改动 | 零侵入 |
| **Service** | 通过 `ServiceManager` 获取 `plain_text_kicker:text_reply`，chatter 显式调用 | 轻侵入 |

## 配置

配置文件：`config/plugins/plain_text_kicker/config.toml`

```toml
[main]
enabled = true                          # 启用事件拦截
request_name_patterns = ["*"]           # glob 匹配请求名
intercept_silence_with_text = true      # 文本 + do_nothing 时也补发文本
max_text_length = 4096                  # 最大发送字符数
enable_dedup = true                     # 启用去重
dedup_window_size = 50                  # 去重窗口大小
debug_log = false                       # 调试日志
```

## Service 用法示例

```python
svc = service_manager.get_service("plain_text_kicker:text_reply")
await svc.try_send("你好呀", stream_id="qq_123")
```

## 核心机制

**注入 `action-do_nothing`：**

在 `AFTER_LLM_REQUEST` 事件处理中，命中拦截条件（纯文本，或文本 + 仅 `do_nothing` 的工具调用）→ `send_text()` 发送 → 保留 `message` → 将 `tool_calls` 替换为：

```python
[{"id": "plain_reply_silence", "name": "action-do_nothing", "args": {}}]
```

框架（`request_execution.py:328-337`）自动将 dict 转为 `ToolCall` 对象。KFC 执行层识别 `action-do_nothing` → 设置 `has_do_nothing=True` → `commit_turn_decision` 中 `has_meaningful_action=True` → 自然 `yield Stop(0)` 收口。

对 `default_chatter` 等不识别 `do_nothing` 的 chatter，该调用被忽略，效果等同于清空 `call_list`。

**工具调用判定：**

拦截前会检查 `tool_calls` 是否「非空且全部为 `do_nothing`」（兼容 `ToolCall` 对象与 dict，名称兼容 `do_nothing` / `action-do_nothing` / `前缀:action-do_nothing`）。只要含有 `kfc_reply` 等其它工具调用，就交回 chatter 正常执行，不做拦截。该行为可用 `intercept_silence_with_text = false` 关闭。

## 兼容性

- **kokoro_flow_chatter (~2.2.2)**：完全兼容，通过控制动作收口
- **default_chatter**：兼容，`do_nothing` 调用被忽略
- **其他基于 `AFTER_LLM_REQUEST` 的 chatter**：理论兼容

## 更新日志

### v1.0.4 (2026-10-11)

- **修复**：模型同时输出回复正文与 `action-do_nothing` 时回复被静默丢弃的问题。原先仅当 `tool_calls` 为空才拦截，导致这类响应既不发送正文（插件判定有工具调用而跳过），chatter 又按沉默收口，用户收不到任何回复。现在「工具调用全是 `do_nothing`」也会被拦截：先发送正文，再用虚假 `plain_reply_silence` 调用替换原始 `do_nothing`。
- **新增**：配置项 `intercept_silence_with_text`（默认 `true`），可关闭上述补发行为。
- **日志**：`debug_log` 开启时输出 `silence_only` 字段；补发命中时输出一条 INFO 日志。
- **说明**：本版修复内容的内部开发版本号为 1.0.2；因市场已发布 1.0.3，发布号顺延为 1.0.4。

### v1.0.1 (2026-08-11)

- **修复**：移除 `AFTER_LLM_REQUEST` 处理器中的 `params["message"] = None`，保留原始消息文本流转至 KFC，确保对话链中正确记录 bot 实际发送的文本内容，避免下一轮 LLM 上下文中丢失已发送消息导致上下文断裂。
