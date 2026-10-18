# plain_text_kicker_qwq — 踹飞纯文本的插件

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

`plain_text_kicker_qwq` 通过监听 `AFTER_LLM_REQUEST` 事件，在响应返回 chatter 之前直接发送纯文本，并用一个虚假的 `action-do_nothing` 调用替换空的 `call_list`，使 chatter 自然收口而不重试。

另有一类丢消息场景：模型**同时**输出了回复正文和 `action-do_nothing`（`call_list` 非空）。此时 chatter 按工具调用执行沉默，正文被丢弃。本插件把「工具调用全是 `do_nothing`」也视为可拦截的纯文本回复，先把正文发出去，再用虚假 silence 调用替换掉原始 `do_nothing`，语义与正常纯文本兜底一致。

第三类场景来自实际日志：`kfc_orchestrator` 报出 `本轮调用列表：['action-kfc_reply']`，紧接着 `kfc_execution` 报 `kfc_reply 的 content 解析后为空，视为工具失败`，本轮直接 Stop——模型把要说的话写在了正文里，却给 `kfc_reply` 传了空 `content`，两边都没发出去。本插件在 `AFTER_LLM_REQUEST` 阶段识别「正文有内容 + 存在非 `do_nothing` 的工具调用」，把正文补发出去，工具调用原样留给 chatter。

## 双模式

| 模式 | 说明 | 侵入性 |
|------|------|--------|
| **EventHandler** | 自动监听 `AFTER_LLM_REQUEST`，无需 chatter 改动 | 零侵入 |
| **Service** | 通过 `ServiceManager` 获取 `plain_text_kicker_qwq:text_reply`，chatter 显式调用 | 轻侵入 |

## 配置

配置文件：`config/plugins/plain_text_kicker_qwq/config.toml`

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
svc = service_manager.get_service("plain_text_kicker_qwq:text_reply")
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

**正常消息只发一次（去重保护）：**

补发前会检查是否存在**已自带非空文本的回复类调用**。命中说明这条消息本来就会由该工具发给用户，插件便不再补发，避免双份：

| 响应形态 | 结果 |
|----------|------|
| 正文「你好」+ `kfc_reply(content=["你好"])` | 工具自己发 → 插件跳过，**1 条** |
| 正文「我在呢」+ `kfc_reply(content="")` | 工具发不出 → 插件补发，**1 条** |
| 正文「看这个」+ `share_visual(visual_id)` | 视觉调用不发文本 → 插件补发，**1 条** |
| 正文「记住」+ `kfc_memo(content="…")` | 备忘只存不发 → 插件补发，**1 条** |

判定分两步，避免误伤：

1. **调用名**必须命中 `TEXT_SENDING_CALL_PATTERNS`（`*kfc_reply*` / `*send_text*` / `*send_message*` / `*reply*`）。像 `kfc_memo` 这种只是把文本存起来、并不发给用户的动作，**即使参数同样叫 `content` 也不算**——否则模型「写正文 + 记备忘」时正文会被吞掉。
2. 该调用的**文本参数**确实非空。参数名取 `TEXT_ARG_KEYS`（`content` / `text` / `message` / `segments` / `msg` / `reply` / `reply_text` / `texts`），判定口径与 KFC 的 `parse_content_segments` 一致（列表逐项取字符串、JSON 数组字符串二次解析）。

## 兼容性

- **kokoro_flow_chatter (~2.2.2)**：完全兼容，通过控制动作收口
- **default_chatter**：兼容，`do_nothing` 调用被忽略
- **其他基于 `AFTER_LLM_REQUEST` 的 chatter**：理论兼容

## 更新日志

### v1.1.2 (2026-10-18)

- **修复（压缩包/市场版装不上）**：市场分发的 manifest 名为 `plain_text_kicker_qwq`，而代码里 `@register_plugin` 注册的 `plugin_name` 还是 `plain_text_kicker`。框架要求两者一致（`PluginManager` 用 `manifest.name` 去注册表取插件类），不一致时压缩包安装后直接报「插件类未注册（未使用 @register_plugin 装饰器）」，插件完全无法加载；源码文件夹方式因为 manifest 与代码同名才显得正常。现统一为 `plain_text_kicker_qwq`：插件名、配置目录（`config/plugins/plain_text_kicker_qwq/config.toml`）、Service ID（`plain_text_kicker_qwq:text_reply`）与日志前缀全部对齐。
- **修复**：注入的假调用名由 `action-do-nothing` 改回 `action-do_nothing`。框架的动作调用名格式是 `action-<name>`（`src/core/components/base/action.py`），KFC 的 `normalize_call_name` 只剥离 `action-` / `tool-` / `agent-` 前缀，写成 `action-do-nothing` 会被归一化成 `do-nothing`：既不被识别为 KFC 控制动作（`has_do_nothing` 不置位），又会被当成第三方调用交给框架去执行一个不存在的 action，白白多一轮与一条失败告警。

### v1.1.1 (2026-10-16)

- **修复**：方案 B 会导致正常消息发两遍。原先只判断「正文非空 + 有工具调用」，于是模型既在正文写了话、又给 `kfc_reply` 传了正常 `content` 时，正文与工具内容会各发一条。
- **新增去重保护**：补发前检查是否存在「已自带非空文本的回复类调用」（调用名命中 `TEXT_SENDING_CALL_PATTERNS`，且 `TEXT_ARG_KEYS` 里有非空文本）。命中则跳过补发，由工具发送，正常消息只发一次。
- **不误伤**：`kfc_memo` 等只消费文本、不发给用户的动作，虽然参数同样叫 `content`，也不计入判定，模型「写正文 + 记备忘」时正文照常补发。
- **日志**：`debug_log` 开启时输出 `reply_call_has_text` 字段；因该保护跳过时输出一条 debug 说明。

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
