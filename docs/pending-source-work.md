# 混合写作义务与条件性人工决策

## 本轮根因和修复范围

基线 `a1f4199ba0a2253b8718ca115f63b79b41be18da` 的真实 BSU 第 7 块未完成独立覆盖审查。
研究现状条款同时要求作者撰写、分类、总结、归纳，以及禁止简单摘录、纳入重复率计算。
原协议的 `source_content_pending` 仅容纳作者写作，不能容纳同条款里的质量核验；
“确实无国外资料时可删除本部分”又被主审归为 informational，遗漏了条件核实和人工选择。
本轮补的是待处理义务的表达与消费链，不是代写、查重、自动删章，也不是降低评分阈值。

## 来源和责任边界

新增 `scripts/pending_source_work.py`，协议为 `source-bound-pending-work/v1`。
只有闭合来源语法才产生最小人工工作清单：

- 有明确正向作者写作指令，且独立命题明确禁止文献资料的简单摘录：反摘录核验。
- 同类写作指令中明确“计算在重复率内”或“计入重复率计算”：范围及报告核验。
- 完整条件及许可明确表示研究确无某类资料时，本部分/节/章可删除：条件核实与章节选择。

条款 ID、学校、证据 ID、原文、偏移、运行身份都来自当前输入，不以 C00113/C00119/C00120
等历史编号建立全局特例。原文、条件和许可保持不变。引述、示例、否定写作、额外条件、
未知表达不由该闭合语法推测授权；未知义务仍须独立语义审查，不能因此被丢弃或自动通过。
重复来源原子无法唯一绑定时拒绝，而非折叠重复项后通过。

代码产生的是清单下限，`execution_authorized=false`；不直接修改独立审查的语义结论。
模型须为每个核验原子选择精确当前 source ref 和 `pending_work_code`，保留独立的作者工作项。
混合条款可返回 `source_content_pending`，内部同时含 `authoring_content_pending` 与
`source_content_verification_pending`。条件性选择使用后者的 pending verdict。
编号枚举仅是版本化协议常量，不表示该核验已完成。

`pending_source_work.source_sha256` 是原样 UTF-8 条款文字的 SHA-256；既有 source-reference
协议仍使用 `sha256_json(document_text)`，evidence source span 的摘要覆盖完整 evidence。
这些口径分别重建校验，不相互替代。当前源码指纹自动包含新模块；旧审查请求不能凭局部
自洽哈希补上新授权清单，必须通过 canonical source packet 重建。

## 重试、义务保全和门禁

独立审查已列全混合 pending 义务但使用错误 verdict 时，最多利用原有两次调用预算进行一次
同候选独立重审；不改候选、引用、原始响应或清单。仍错误即失败，不增加无限重试。
条件性条款被主审误分为 informational 时，沿用来源和父候选哈希绑定的分类纠正路径，
只允许 classification 改为 `requires_source_verification`。新要求、改写 reason、错误父哈希、
错引来源等均不在该授权范围内，纠正后仍需重新审查。

没有把历史 `unrepresented` 自动改成 pending。`incomplete` 可保留为诊断结果，桥接器拒绝接受；
本轮也在 pipeline 输出策略补上同样拒绝，不能将 incomplete 回执用于生成或提交。
合法 pending 只允许显式 `review_draft`，submission 继续阻塞。

桥接 AO ledger 和消费端重建均保留 `pending_work_code`，每项绑定当前 run、case、chunk、
attempt、候选/请求/响应摘要及精确 source ref/范围。消费端逐项生成 release gate，保留
写作、反摘录、查重范围、条件性选择各自的行动说明。完整 gate 进入 MO/MR 与 producer record，
再成为序列化 DOCX 中的独立红色标记。各项仍为 pending，`submission_ready=false`。
这一链只记录工作与来源，未建立“人工已签核”的新放行功能。

## 验证与尚未完成的事项

专门回归：`tests/test_pending_source_work.py`。
本轮验证结果：五个相关测试模块合计 `59 passed, 128 subtests passed`；
整仓 `.venv/bin/python -m pytest -q` 为 `1112 passed, 6 skipped, 533 subtests passed`。
Python 编译检查和 `git diff --check` 通过。跳过项不计为通过，测试中的模拟 provider 不代表真实调用。
覆盖闭合语法、引述/示例/否定/条件反例、已注册清单缺项、错码/重复码、错误 quote/ref、
旧源摘要、wrong verdict、空清单、真实 primary validator 与受限分类重试、独立重审预算、
不自动转译 unrepresented，以及 receipt → AO → gate → MO/MR → DOCX 红标的离线链。
国内/国外来源分别保留四项作者义务及两项核验义务，条件性选择单独保留；不合并成一个模糊工作项。
消费端也负测错误运行、来源摘要、证据、工作码和重复 AO。

只读子代理分别复核来源语法/validator，以及账本/消费链。来源核验发现消费端未直接拒绝
incomplete，已补修并加入反例；不能据此声称原先整个发布链可绕过，因为主桥接已拒绝 incomplete。
另一复核在限定范围内未发现字段丢失或错绑，但未执行真实 provider/BSU。

历史回放使用 `build/fresh-bsu-codex-luna-20260930T101253Z-a1f4199` 的第 7 块。
输入 requirements DOCX SHA-256 核对为
`1e9d38b307dbef8c3bf97b85ba5def70eaf7e2dba50cf25da978e007fc907059`。
旧 incomplete 继续拒绝；在内存中构造、明确标注为提案的 pending 解释通过 20 条款校验，
其中三条涉及的 13 个原子全部保留（8 项作者工作、5 项人工核验），requirement 图不变，submission 仍拒绝。
这不是生产自动投影，不是新模型回答，也没有生成新的真实回执。两轮旧响应、原始主审、候选、
chunk request 和 host-agent-run 文件摘要检查前后相同，未回写或重新盖章。

离线 DOCX 的红标结构审计不是 Word 视觉验收。本轮未 commit、push、启动真实 BSU 或其余九校，
未运行真实 Word/PDF。后续获批使用新目录和原生 Codex重跑 BSU；请求模型按项目当前默认
`gpt-6-luna`（2026-10-01 更新），不改变上述历史回放的模型记录；
只有实际合并、草稿生成和后验检查通过才算本轮真实 BSU 通过。仍可能出现其他尚未观察的阻断。
