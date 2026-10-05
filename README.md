# SiliconRail

这是一个面向芯片与半导体设计的芯片前端设计与 RTL 流程工具链。长期目标是提供 RTL 解析与电路 IR、位宽推演、语义检查、综合到门级网表、静态时序分析、面积与功耗估算、等价性检查和形式验证，把芯片前端流程沉淀为可复用工具链。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m siliconrail.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `SILICONRAIL_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## RTL 解析

`Service.parse_rtl(source)` 将单个 Verilog-2001 源文本解析为可 JSON 序列化的电路 IR，源文本内允许出现多个 module。支持大小写敏感标识符、十进制与带位宽的二进制/十六进制常量、行注释与块注释、ANSI 风格 `input`/`output`/`inout` 端口（可带 `wire` 关键字）、`wire` 声明、连续 `assign`，以及括号、位选、常量范围片选、拼接和常见一元/二元运算。

- 成功返回 `{"modules": [...]}`，模块按源码顺序排列；端口与网络带规范化 `width`（省略范围为 1，`[msb:lsb]` 为 `abs(msb-lsb)+1`），连续赋值按源码顺序排列。
- 直接入口：`source` 不是字符串抛 `TypeError`；空字符串与一切词法/语法/语义失败抛 `siliconrail.rtl.RTLParseError`，携带 `code`、1 起始的 `line`/`column` 与非空 `message`。`code` 取值：`syntax_error`、`duplicate_name`、`undeclared_signal`、`invalid_range`、`unsupported_construct`。
- HTTP：`POST /v1/rtl/parse`，请求体为 `{"source": "..."}`。成功返回 200 且 IR 与直接入口完全一致；请求体不是 JSON 对象、缺少 `source` 或 `source` 不是字符串（含无效 UTF-8、畸形 JSON）返回 400 `invalid_request`；解析失败返回 422 并在 `error` 中原样返回 `code`/`line`/`column`/`message`。

过程块、实例与参数化语法属于子集之外，返回 `unsupported_construct`。

## 位宽推演

`Service.analyze_widths(source)` 在同一次入口中完成解析与无符号位宽推演，成功结果仍以 `{"modules": [...]}` 为顶层，模块、端口、网络、连续赋值的源码顺序与原有 IR 字段全部保留，表达式树与常量值不被改写。

- 赋值目标与值中的每个表达式节点增加整数 `width`；每条连续赋值增加 `target_width`、`value_width` 与 `conversion`。`conversion` 取值为 `exact`（等宽）、`zero_extend`（值窄于目标）或 `truncate`（值宽于目标，语义上保留低 `target_width` 位）。
- 推演规则（信号一律按无符号处理）：引用与带位宽常量采用声明宽度；无位宽十进制常量采用容纳其值且不小于 32 的宽度（`0` 为 32 位）；位选为 1 位；常量范围片选为 `abs(msb-lsb)+1`；拼接为各项宽度之和；逻辑非、归约运算、逻辑与/或与比较运算为 1 位；一元 `+`/`-`/按位取反沿用操作数宽度；移位采用左操作数宽度；其余受支持二元运算采用两侧较大宽度。
- 失败语义与 `parse_rtl` 完全一致：非字符串抛 `TypeError`；空文本与词法/语法/重复名称/未声明信号/非法范围/不支持构造抛 `RTLParseError`，`code`/`line`/`column`/`message` 语义不变。
- HTTP：`POST /v1/rtl/widths`，请求与响应约定与 `/v1/rtl/parse` 相同（非 JSON 对象、缺少 `source`、`source` 非字符串、无效 UTF-8 或畸形 JSON 返回 400 `invalid_request`；解析失败返回 422 并原样给出四个错误字段；额外请求字段允许）。`POST /v1/rtl/parse` 的响应保持不变。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线在健康检查之外提供 Verilog-2001 组合逻辑子集的 RTL 解析、电路 IR（见上）与无符号位宽推演，尚未包含语义检查、静态时序分析等后续能力，它们将从已冻结事实出发独立设计并验证。
