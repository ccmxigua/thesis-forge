# 当前来源表格结构与有界重审

## 责任边界

表格中的日期占位不能只按孤立文字解释，也不能由代码把任意左侧文字猜成某个元数据字段。本轮增加的是来源结构证据和同一候选的独立重审，不是自动选择日期含义或自动通过。

- `requirements_engine.extract_document_evidence()` 从当前 DOCX 的物理行、单元格和段落生成 `table_row_context`。保留空格、空白单元格、列跨度、横向/纵向合并、网格前后空位及嵌套表格标志。每段包含当前 evidence ID 和文本哈希，每行包含完整行快照哈希。
- 行快照包含在 canonical evidence/request 的哈希中，并随目标 evidence 跨 chunk 保留；主审 compact packet 也不丢弃它。
- `build_obligation_coverage_request()` 从当前 evidence 构造 `table_structure_context`，检查目标文本、证据身份、坐标、行哈希及当前可用邻格 evidence。邻格文字只作结构上下文，不能扩充 `document_text` 或成为本条款的来源引用。
- 只有完整网格、非 RTL 视觉顺序、无合并/嵌套、零前后空位、左右单元格各有唯一非空段落时，才声明“同行紧邻左格”。这仍不是语义字段绑定。
- 无此快照的旧证据保持原契约，不从 `context_before`、条款 ID、学校名称或自由文字升级为可靠结构。

## 重审与门禁

`table_structure_context_uncertainty` 仅用于完整中文日期占位 `年 … 月 … 日`：候选仍为 executable、有对应要求，独立审查仅返回 ambiguous/uncertain，没有未表示义务、已表示义务或 scope_unresolved，且当前结构绑定有效。

桥接器最多允许一次同候选独立重审，使用现有全局两次独立调用预算，不增加无限重试。候选的 classification、requirements、properties、证据、原始响应均不修改。第二次仍不确定或发现遗漏就失败；不得转成通过或启动九校。

桥接回执和 pipeline 消费端都复核新错误码的条款范围、当前结构授权和 `provider_attempt=2`。完整 request 必须可从 canonical source packet 与已接受候选重建。局部自洽的行哈希不是来源真实性证明：即使攻击者改写邻格、重签 request/envelope 哈希，下游 canonical reconstruction 仍拒绝。分析 ledger 继续绑定完整 review request 哈希，不代表 submission_ready。

## 离线验证边界

专门回归：`tests/test_table_structure_review.py`；发布回执负例：`tests/test_host_review_commit_marker.py`。

覆盖跨 chunk/compact 保全、空格及精确引用、任意字段名、合并/嵌套/空白/多段/缺格拒绝、旧行哈希、跨行/跨 part 错绑、组合错误、候选不变、次数耗尽、伪造 typed error、有效第二次审查的来源编译/回执重建，以及重签邻格篡改拒绝。

真实 BSU 历史证据位于 `build/fresh-bsu-codex-luna-20260930T085029Z-92984e8`。离线重新提取当前 requirements DOCX，并核对 source SHA-256 为 `1e9d38b307dbef8c3bf97b85ba5def70eaf7e2dba50cf25da978e007fc907059`、每个旧 evidence 的文字和坐标未变。C00049 的当前结构为 table child 45 / row 3 / column 3，左邻 column 2 是“批准日期”；该行另有“审批表编号”和一个空白单元格。

该离线检查生成新诊断身份，不重新封装或接受旧运行回执。旧 uncertain 仍被拒绝，只是获得新有界重审错误码。旧 chunk request、candidate 与独立 compiled response 文件哈希未改变。

离线通过不能证明下一次 Luna 会正确使用结构，也不能代替真实 BSU、Word/PDF 验收或发布通过。真实重跑和 push 另行执行，不把它们记作本轮已完成。
