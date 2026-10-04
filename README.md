# SiliconRail

这是一个面向芯片与半导体设计的芯片前端设计与 RTL 流程工具链。长期目标是提供 RTL 解析与电路 IR、位宽推演、语义检查、综合到门级网表、静态时序分析、面积与功耗估算、等价性检查和形式验证，把芯片前端流程沉淀为可复用工具链。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m siliconrail.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `SILICONRAIL_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## RTL 解析

`POST /v1/rtl/parse` 接受 JSON 对象 `{"source": "<verilog 源文本>"}`，成功时返回 200 与可 JSON 序列化的电路 IR（`modules` 按源码顺序，含声明顺序的端口/网络与源码顺序的连续赋值）。`Service.parse_rtl(source)` 提供等价的直接入口，非字符串输入抛 `TypeError`。

首版支持单源文本内 Verilog-2001 组合逻辑子集（可多 module）：大小写敏感标识符、十进制与带位宽的二进制/十六进制常量、行/块注释、ANSI 风格 `input`/`output`/`inout` 端口、`wire`、连续 `assign`，以及括号、位选、常量范围片选、拼接和常见一元/二元运算。

解析失败时直接入口抛出公开的 `RTLParseError`（携带 `code`、从 1 开始的 `line`/`column` 与非空 `message`）；HTTP 映射为 422 并在 `error` 中原样返回这四项。错误码：`syntax_error`（词法错误、括号或语句不完整、空源文本）、`duplicate_name`（模块重名或模块内端口/网络重名）、`undeclared_signal`、`invalid_range`、`unsupported_construct`（过程块、实例、参数化语法）。请求体非 JSON 对象、缺少 `source` 或 `source` 非字符串（含无效 UTF-8、畸形 JSON）时返回 400 且 `error.code` 为 `invalid_request`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含RTL 解析、电路 IR 与静态时序分析的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
