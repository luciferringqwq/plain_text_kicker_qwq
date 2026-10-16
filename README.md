# plain_text_kicker — 踹飞纯文本的插件

在 `AFTER_LLM_REQUEST` 事件中截获模型非工具调用的纯文本输出，直接发送为聊天回复，并注入虚假 `action-do_nothing` 工具调用阻止 chatter 重试循环。

同时覆盖「回复文本 + `action-do_nothing`」的组合：部分模型会把回复写进 message 正文，却又调用 `do_nothing` 表示沉默。chatter 只执行工具调用，message 会被静默丢弃（用户收不到任何回复），因此本插件也把这类文本补发出去。

最后覆盖「回复文本 + 其它工具调用」的组合（方案 B）：模型给出正文的同时又调用了真实工具。chatter 只认工具调用，正文被静默丢弃；若那个工具的文本参数还是空的（最典型的是 `kfc_reply` 收到空 `content`，执行层直接判「content 解析后为空，视为工具失败」），用户就一个字都收不到。本插件把这类正文补发出去，工具调用本身原样交回 chatter 执行。

## 解决的问题

部分 chatter（如 **kokoro_flow_chatter**）在 LLM 返回纯文本（而非 JSON 工具调用）时会进入重试循环：
```
if not response.call_list:         # ← 没有 tool_call？
    inject retry reminder           #    注入"请用 tool_call"提示
    continue                        #    回到循环顶重新请求 LLM
```

`plain_text_kicker` 通过监听 `AFTER_LLM_REQUEST` 事件，在响应返回 chatter 之前直接发送纯文本，并用一个虚假的 `action-do_nothing` 调用替换空的 `call_list`，使 chatter 自然收口而不重试。

另有一类丢消息场景：模型**同时**输出了回复正文和 `action-do_nothing`（`call_list` 非空）。此时 chatter 按工具调用执行沉默，正文被丢弃。本插件把「工具调用全是 `do_nothing`」也视为可拦截的纯文本回复，先把正文发出去，再用虚假 silence 调用替换掉原始 `do_nothing`，语义与正常纯文本兜底一致。

第三类场景来自实际日志：`kfc_orchestrator` 报出 `本轮调用列表：['action-kfc_reply']`，紧接着 `kfc_execution` 报 `kfc_reply 的 content 解析后为空，视为工具失败`，本轮直接 Stop——模型把要说的话写在了正文里，却给 `kfc_reply` 传了空 `content`，两边都没发出去。本插件在 `AFTER_LLM_REQUEST` 阶段识别「正文有内容 + 存在非 `do_nothing` 的工具调用」，把正文补发出去，工具调用原样留给 chatter。

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
intercept_tool_calls_with_text = true   # 文本 + 其它工具调用时也补发文本（方案 B）
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

**三类拦截模式：**

| 模式 | 命中条件 | `tool_calls` 处理 |
|------|----------|-------------------|
| `plain_text` | 正文非空、无工具调用 | 整体替换为假 `action-do_nothing` |
| `silence_with_text` | 正文非空、调用全是 `do_nothing` | 整体替换为假 `action-do_nothing` |
| `tool_calls_with_text` | 正文非空、含其它工具调用 | 原样保留，交回 chatter 执行 |

前两类都注入同一个假调用（框架 `request_execution.py:328-337` 自动把 dict 转为 `ToolCall` 对象）：

```python
[{"id": "plain_reply_silence", "name": "action-do_nothing", "args": {}}]
```

KFC 执行层识别 `action-do_nothing` → 设置 `has_do_nothing=True` → `commit_turn_decision` 中 `has_meaningful_action=True` → 自然 `yield Stop(0)` 收口。对 `default_chatter` 等不识别 `do_nothing` 的 chatter，该调用被忽略，效果等同于清空 `call_list`。

**第三类（方案 B）不做任何替换**：只把正文补发出去，`tool_calls` 原样交回 chatter 正常执行——换装、发表情等调用不会被吞掉。可用 `intercept_tool_calls_with_text = false` 关闭。

注意：该模式只看「正文非空 + 存在非 `do_nothing` 调用」，**不检查工具调用自己是否已经带了文本**。若模型既在正文里写了话、又给 `kfc_reply` 传了正常 `content`，正文与工具内容会各发一条。这是刻意的简单实现；如需「工具已带文本就不重复补发」的保护，需要再加判断。

## 兼容性

- **kokoro_flow_chatter (~2.2.2)**：完全兼容，通过控制动作收口
- **default_chatter**：兼容，`do_nothing` 调用被忽略
- **其他基于 `AFTER_LLM_REQUEST` 的 chatter**：理论兼容

## 更新日志

### v1.1.0 (2026-10-15)

- **新增（方案 B）**：拦截「正文 + 其它工具调用」的响应并补发正文。实际日志中 `kfc_orchestrator` 报 `本轮调用列表：['action-kfc_reply']`、紧接着 `kfc_execution` 报 `kfc_reply 的 content 解析后为空，视为工具失败`，本轮直接 Stop——模型把要说的话写在正文里，却给 `kfc_reply` 传了空 `content`，结果两边都没发出去。现在这类正文会被补发。
- **行为**：该模式只补发正文，`tool_calls` 原样交回 chatter 执行，不做整体替换，也不摘除任何调用。
- **新增**：配置项 `intercept_tool_calls_with_text`（默认 `true`），可关闭该补发行为。
- **日志**：`debug_log` 开启时输出 `mode` 字段；该模式补发命中时输出一条 INFO 日志（含原始调用列表）。

### v1.0.4 (2026-10-11)

- **修复**：模型同时输出回复正文与 `action-do_nothing` 时回复被静默丢弃的问题。原先仅当 `tool_calls` 为空才拦截，导致这类响应既不发送正文（插件判定有工具调用而跳过），chatter 又按沉默收口，用户收不到任何回复。现在「工具调用全是 `do_nothing`」也会被拦截：先发送正文，再用虚假 `plain_reply_silence` 调用替换原始 `do_nothing`。
- **新增**：配置项 `intercept_silence_with_text`（默认 `true`），可关闭上述补发行为。
- **日志**：`debug_log` 开启时输出 `silence_only` 字段；补发命中时输出一条 INFO 日志。
- **说明**：本版修复内容的内部开发版本号为 1.0.2；因市场已发布 1.0.3，发布号顺延为 1.0.4。

### v1.0.1 (2026-08-11)

- **修复**：移除 `AFTER_LLM_REQUEST` 处理器中的 `params["message"] = None`，保留原始消息文本流转至 KFC，确保对话链中正确记录 bot 实际发送的文本内容，避免下一轮 LLM 上下文中丢失已发送消息导致上下文断裂。
