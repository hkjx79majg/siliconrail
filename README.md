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

`Service.analyze_widths(source)` 对同一源文本完成解析与无符号位宽推演，返回的 IR 与 `parse_rtl` 结构一致（`modules` 顶层、源码顺序、原有字段不变），并为赋值目标与值中的每个表达式节点增加 `width`，为每条赋值增加 `target_width`、`value_width`、`conversion`（`exact` / `zero_extend` / `truncate`）。

- 引用与带位宽常量取声明宽度；无位宽十进制常量取容纳其值且不小于 32 的宽度（零为 32 位）。
- 位选为 1 位，常量范围片选为 `abs(msb-lsb)+1`，拼接为各项宽度之和。
- 逻辑非、归约、逻辑与比较运算结果为 1 位；一元加减与按位取反沿用操作数宽度；移位取左操作数宽度；其余二元运算取两侧较大宽度。
- 值较目标窄为 `zero_extend`，较宽为 `truncate`（仅标注，不改写表达式树或常量值），相等为 `exact`。
- 失败语义与 `parse_rtl` 完全相同：非字符串抛 `TypeError`，解析失败抛同一 `RTLParseError`。HTTP 入口为 `POST /v1/rtl/widths`，请求约定与错误响应（400 `invalid_request` / 422 四字段）同 `/v1/rtl/parse`。

## 跨时钟域结构检查

`Service.check_cdc(design, constraints=None)` 对已经完成层次展开和参数求值的设计执行结构性 CDC 检查，返回可 JSON 序列化的确定性报告；约束只影响本次报告，不改写输入设计。HTTP 入口为 `POST /v1/cdc/check`，请求体为 `{"design": {...}, "constraints": {...}?}`。

**设计格式**（JSON 安全的 dict，扁平网表）：

- `top`（可选字符串）、`elaborated`（可选；显式为 `false`，或仍含 `modules` / 非空 `instances` / 非空 `parameters` 时视为未展开，抛 `ValueError`）。
- `clocks`：`{"name": "clk"}` 声明根时钟；`{"name": "div", "derived_from": "clk", "divide_by": 2}` 声明经可识别整数分频、相位关系明确的派生时钟。
- `ports`：`{"name", "direction", "width"?}`，`input`/`inout` 端口是 `external` 域的潜在穿越源。
- `nets`（可选）：`{"name", "width"?, "const"?, "gray_code"?}`，声明线网位宽、常量绑定与格雷码标记。
- `cells`：`dff`（`clk`/`d`/`q`，可选 `edge`、`async_reset`/`async_set`、`width`、`gray_code`、`source`）、`logic`（`inputs`/`output`）、`memory`（`write_clock` 必填，可选 `write_enable`/`write_data`/`write_addr`/`read_data`/`read_addr`）。`source` 为 `{"module", "line", "column"}` 等 RTL 来源信息，报告原样带回。

**约束格式**（可选 dict）：`async_clock_groups`（组内同步、组间异步）、`synchronous_clocks`（组内声明同步）、`quasi_static`（层次名列表，穿越被抑制）、`resets`（`{"name", "clock"?}`，声明后对应复位释放穿越降为 info）。约束引用设计中不存在的对象抛 `KeyError`；同一对时钟同时被声明为同步和异步抛 `ValueError`。

**域关系**：同一时钟（含不同边沿）为同步；未提供异步关系时不同根时钟按潜在异步处理；同一根时钟经整数分频的域按同步处理。同步域之间不产生诊断。

**报告**：`{"top", "domains", "clock_relations", "diagnostics", "summary"}`。`diagnostics` 按 `(source, dest)` 层次路径稳定排序，每条含 `source_domain`、`dest_domain`、`source`、`dest`、`width`、`category`、`severity`（`error`/`warning`/`info`）、`message`、`source_info`/`dest_info`。类别：`synchronized`（单比特、至少两级、同一目的时钟、级间无组合逻辑的寄存器链，info）、`sync_chain_fanout`（同步链第一级扇出到其他逻辑，error）、`state_element`、`combinational`、`memory_write_control`、`async_reset_release`（均 error）、`multi_bit_coherence`（多比特总线逐位双触发器仍有位间一致性风险，warning）、`gray_code`（已识别的格雷码穿越，info）。同一源端点到目的端点的重复汇合只保留一条（取最高严重级别）；同域路径、常量与准静态对象不产生诊断；空设计与纯组合设计返回零诊断报告。

**失败语义**：`design` 非 dict 抛 `TypeError`；输入未展开、时钟连接无法解析（如时钟由逻辑驱动、派生时钟根缺失或分频比非正整数）、约束自相矛盾抛 `ValueError`；约束引用不存在的层次对象抛 `KeyError`。设计本身存在不安全穿越属于分析结果，不导致调用失败。HTTP 侧：请求体非法返回 400 `invalid_request`，`ValueError`/`KeyError` 分别返回 422 `invalid_design` / `unknown_object`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线在健康检查之外提供 Verilog-2001 组合逻辑子集的 RTL 解析、电路 IR 与无符号位宽推演（见上），以及面向已展开设计的跨时钟域结构检查；尚未包含静态时序分析等后续能力，它们将从已冻结事实出发独立设计并验证。
