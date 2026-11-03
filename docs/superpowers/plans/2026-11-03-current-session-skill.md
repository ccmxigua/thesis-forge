# Thesis Current-session Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让当前 dot 会话直接运行可移植 Thesis skill，生成有独立内部审阅及 WPS 限定原生证据的审查稿，并如实保留 Word 发布边界。

**Architecture:** 复用现有 packet → merge → deterministic pipeline，不另建语义引擎。新增薄的能力/会话/审阅/原生证据接口；当前会话负责模型与浏览器操作，脚本只负责确定性处理，不能暗中调用另一个模型 CLI。

**Tech Stack:** Python；现有 python-docx/lxml/PyMuPDF/Pillow/pypdf/PyYAML；按输入按需使用 Pandoc、LibreOffice、Poppler；受支持的 dot 内部审阅及云 WPS 工具。

**Spec:** `docs/superpowers/specs/2026-11-03-current-session-skill-design.md`

## Global Constraints

- 基线提交固定为 `7d0d3843517d75fe5fe625d3009ef0e93b8c8957`；原分支 `fix/uno-toc-glyph-v36-20261101` 只作来源，不覆盖其历史。
- 建议项目分支 `feat/current-session-skill-20261103`；若已存在先核内容，不强制覆盖、不 force push。
- 当前会话默认不调用 Codex/OpenClaw/其他模型 CLI，不改全局配置、不新增 API key；未知实际模型身份记为不可观察。
- 最多两个测试候选，每候选最多两次完整本地渲染及一次 WPS 保存关闭重开验收。所有真实渲染含测试/scratch 都计账；预算耗尽停止。
- 不覆盖真实论文、原模板、V21；I045 等作者判断项保留。不得静默修改数学、删除空白页、替换 OLE 或伪造 Word 发布证明。
- 测试后 push 项目分支，不 merge；远端源码、安装、当前 dot 真实运行三项分别验收。
- 新增接口均为本计划要求，不能声称现有代码已具备。现有命令和内部状态文件仍是权威，薄接口只投影和调度，不另造并行状态机。

## Review Focus

1. 包离开原仓库目录后漏资源或读到原工程文件：任务1的隔离解包闭包测试。
2. 同名文件换字节、代码漂移、重复 resume：任务2的来源绑定和幂等回归。
3. 作者伪装审阅者、旧候选回执或不可见图片：任务3拒绝，不改正式发布旗标。
4. Normal 样式但确认为标题的段落、直接 keepNext=0、罗马页码/分节目录：任务4来源绑定分页和 native-only 分流。
5. WPS 上传/导出结果 unknown、在线版本变动、预算最后一次：任务5的原生身份和预留/核销测试。

## 文件与跨任务接口

复用 `scripts/thesis_format.py`、`merge_host_agent_review.py`、`offline_review_receipt.py`、`thesis_format_pipeline.py`、`apply_format_spec.py`、`toc_materializer.py`；保留 `word_render_export.py`、`post_render_acceptance.py` 的原有正式门禁。

新增职责分离模块：

- `scripts/skill_bundle.py`：打包/清单校验；`skill-package.json` 声明入口及完整运行资源。
- `scripts/skill_runtime.py`：只读能力检测，无安装、网络、模型、渲染副作用。
- `scripts/current_session.py`：现有流程的 run/status/resume/next 投影。
- `scripts/session_review.py`：独立任务绑定、主机观察证据核对、内部审阅状态。
- `scripts/native_acceptance.py`：WPS 证据及与 Word 分离的验收状态。
- `scripts/validation_budget.py`：本次有限验证的原子预留、完成及不确定状态。
- 对应 `schema/current-session-v1.schema.json`、`internal-review-v1.schema.json`、`native-acceptance-v1.schema.json`。

固定公共数据：`SessionBinding = {run_id, requirements_sha256, input_sha256, code_sha256, candidate_sha256}`；生成前 `candidate_sha256=null`，生成后必须为精确字节。所有接受回执还绑定 `task_id`、`role`、`artifact_manifest_sha256`。未知字段按 schema 拒绝，版本变化不能静默升级旧回执。

`SessionStatus = {binding, next_action, blocked_reasons, internal_review, native_checks, word_release, budgets}`。内部审阅/WPS 状态不得改写旧流程的 `provider_model_verified`、`independent_review_verified` 或 `submission_ready`，除非原有正式 verifier 本身证明其条件；本次默认 `submission_ready=false`。

### Task 1: 冻结代码并形成可离线使用的完整包

**Files:** 新增 `skill-package.json`、`requirements-runtime.txt`、`scripts/skill_bundle.py`、`scripts/skill_runtime.py`、`tests/test_skill_bundle.py`、`tests/test_skill_runtime.py`；更新 `SKILL.md`、`README.md`、`docs/install-dot.md`。

**Interfaces:** `build_bundle(repo_root: Path, output: Path) -> dict`；`verify_bundle(root: Path) -> dict`；`probe_capabilities(root: Path, requested: list[str]) -> dict`。报告含包版本、逐文件 SHA256、入口、真实工具/依赖版本、缺项；probe 不创建业务工作区。

- [ ] 核真实仓库 HEAD、工作树和分支；保全未提交内容。在隔离工作树携带本 spec/plan。记录基线与工作树差异，不从临时文档交接目录执行产品命令。
- [ ] 写失败测试，人工生成无私人数据的最小包夹具。测试接口如下，缺失模块时应红：

```python
def test_relocated_bundle_has_no_repository_dependency(bundle_fixture, tmp_path):
    moved = bundle_fixture.extract_to(tmp_path / "different root")
    assert verify_bundle(moved)["status"] == "pass"
    (moved / "schema" / bundle_fixture.required_schema).unlink()
    assert verify_bundle(moved)["status"] == "blocked"

def test_probe_never_runs_provider_or_renderer(probe_fixture):
    report = probe_fixture.probe_without_optional_tools()
    assert report["capabilities"]["word_release"]["status"] == "unavailable"
    assert probe_fixture.model_calls == probe_fixture.render_calls == 0
```

- [ ] 运行 `python -m pytest tests/test_skill_bundle.py tests/test_skill_runtime.py -q`，确认失败指向缺失功能，不是外部环境异常。
- [ ] 实现清单、可重复打包和只读 probe。逐项检查原入口对 scripts/schema/resources、转换 wrapper、filter、模板等的真实引用；使用显式许可清单，禁止将 build/、凭据、私人测试稿或 `.git` 打包。Python 依赖从基线 requirements-test.txt 拆出运行项，保留已固定版本，pytest 仍只在测试依赖；不随意升级版本。安装说明使用项目独立虚拟环境和受支持官方来源，不改全局模型/认证配置；OS工具缺失必须显式报告或走已批准的正常安装入口，不能宣称已随skill自带。声明实际验证的 Python/平台组合，不沿用未经兼容验证的旧最低版本承诺。
- [ ] 重跑上述测试；从解包根调用 probe，故意隐藏原仓库路径和 optional executables，仍能报告准确缺项。此处不运行业务生成/渲染。
- [ ] 独立代码审阅并 commit：`feat: package portable thesis skill and capability probe`。

### Task 2: 当前会话入口、恢复与预算守卫

**Files:** 新增 `scripts/current_session.py`、`scripts/validation_budget.py`、`schema/current-session-v1.schema.json`、`tests/test_current_session.py`、`tests/test_validation_budget.py`；仅在必要处修改 `scripts/thesis_format.py` 的安全入口。

**Interfaces:** `inspect_session(work_dir: Path) -> SessionStatus`；`next_action(work_dir: Path) -> dict`；`resume_session(work_dir: Path) -> SessionStatus`。CLI：`current_session.py run REQUIREMENTS INPUT --work-dir DIR --campaign DIR`、`status --work-dir DIR`、`next --work-dir DIR`、`resume --work-dir DIR`。run 仅调用既有准备入口；需要语义响应/审阅/浏览器时返回任务，不伪装自动完成。

`reserve_operation(campaign: Path, candidate_id: str, kind: str, operation_id: str) -> dict`；`record_operation(campaign: Path, operation_id: str, outcome: str, evidence: dict) -> dict`。kind 为 candidate/full_render/wps_acceptance；同 ID 重放不重复扣费，unknown 保持预留，只有已证未执行才能解除。

- [ ] 写回归：准备不启动 provider；正常语义包 merge 仍接受；缺 chunk 阻塞；相同文件名不同字节拒绝；代码漂移拒绝；同一 pending task 重复 resume 不重新生成/不收费；上限外预留拒绝且无工具副作用。

```python
def test_resume_rejects_changed_source(session_fixture):
    session_fixture.change_input_bytes()
    before = session_fixture.snapshot()
    assert resume_session(session_fixture.path)["next_action"] == "blocked"
    assert session_fixture.snapshot() == before

def test_third_candidate_is_rejected(budget_fixture):
    budget_fixture.complete_candidates(2)
    assert budget_fixture.reserve_candidate("c3")["status"] == "blocked"
    assert budget_fixture.started_operations == 2
```

- [ ] 运行 `python -m pytest tests/test_current_session.py tests/test_validation_budget.py -q`，确认预期红。
- [ ] 实现薄投影，从现有 extraction/merge/manifest 获取身份；保留既有来源和代码校验。所有完整渲染及 WPS验收在副作用前先预留。预算账在 campaign 层，不能通过新目录绕开两个候选上限。无法确定调用是否执行时先对账，不盲重试。
- [ ] 重跑回归，并以 mock subprocess 断言普通 run/resume 没有 codex/openclaw、网络或原工程 thread 调用。原 `thesis_format.py` 显式 legacy 参数行为保持不变。
- [ ] 独立审阅并 commit：`feat: orchestrate current-session thesis runs with bounded resume`。

### Task 3: 接入内部独立审阅，保持原正式门禁

**Files:** 新增 `scripts/session_review.py`、`schema/internal-review-v1.schema.json`、`tests/test_session_review.py`；修改 `scripts/current_session.py`、`SKILL.md` 的审阅任务路由。

**Interfaces:** `issue_review(work_dir: Path, role: str) -> dict`；`import_review(work_dir: Path, task_id: str, observation: dict) -> dict`；角色为 `source_format`、`visual`。observation 包含真实主机工具来源引用、作者/审阅任务身份、原始结果摘要 hash、实际读文件/图片记录和逐项判断。缺主机可观察身份时为 `pending/unverifiable`，不能接受作者自写的名字作为独立证明。

- [ ] 写测试矩阵：不同任务且正确绑定 PASS；作者同身份拒绝；旧候选/旧任务/过期回执拒绝；视觉记录缺必需页面或图片不可观察则待核；HOLD 不关闭问题；修后旧回执失效；上述任何输入均不得把 Word release 或 submission_ready 改成 true。

```python
def test_internal_pass_does_not_mint_formal_release(review_fixture):
    out = import_review(review_fixture.work, review_fixture.task_id,
                        review_fixture.observed_distinct_actor_pass())
    assert out["internal_review"]["status"] == "pass"
    assert out["submission_ready"] is False
    assert out["provider_model_verified"] is False
```

- [ ] 运行 `python -m pytest tests/test_session_review.py -q`，确认红。
- [ ] 实现来源/候选快照与严格 importer；当前会话通过真实内部审阅工具发任务、接结果，再调用 importer。先核平台实际能提供的来源字段，不设计不存在的 API。工具观察不可核实时拒绝“独立通过”，不得仿造签名或以 mock adapter 投产。模拟测试必须标识 fixture 来源且不能写生产接受回执。
- [ ] 将只读审阅与修复分开；修复由有写权限的执行者生成新候选，fresh reviewer 才能关闭问题。保留原 offline merge 的正式 unverified 状态。
- [ ] 重跑针对性测试和既有 offline receipt/host review 相关回归；独立审阅并 commit：`feat: bind internal review to exact thesis candidates`。

### Task 4: 修通标题与目录流程，但不绕过原生差异

**Files:** 修改 `scripts/apply_format_spec.py`、`scripts/current_session.py`；必要时仅补 `scripts/toc_materializer.py` 的能力报告，不扩大其未经验证的适用域。新增 `tests/test_heading_keep_next.py`、`tests/test_current_session_toc_routing.py`。

**Interfaces:** 在现有格式应用层增加 `apply_heading_keep_with_next(doc, bindings: list[dict], *, enabled: bool) -> list[dict]`。bindings 必须来自当前来源/角色解析，包含段落定位、期望原段落 hash、标题角色和规则来源；仅 enabled=true 且绑定核对通过才能修改直接段落属性。返回逐段变更回执。

- [ ] 先读现有角色映射与规则解析调用点，写失败测试：来源确认标题但样式 Normal、直接 keepNext=0 时，已批准 keep_with_next=true 应真正生效；无标题/规则证据不能猜改；明确 false 必须尊重；正文、分页符、OLE 和字体内容不变。

```python
def test_direct_false_cannot_override_bound_true_heading_rule(heading_fixture):
    apply_heading_keep_with_next(heading_fixture.doc, heading_fixture.bindings,
                                 enabled=True)
    assert heading_fixture.heading.paragraph_format.keep_with_next is True
    assert heading_fixture.protected_xml_unchanged()

def test_roman_restart_toc_requires_native_refresh(toc_fixture):
    status = toc_fixture.status_with_roman_frontmatter_and_restart()
    assert status["toc_action"] == "native_refresh_required"
    assert toc_fixture.materializer_calls == 0
```

- [ ] 运行 `python -m pytest tests/test_heading_keep_next.py tests/test_current_session_toc_routing.py -q`，确认真实遗漏或将已满足行为固定为回归；已有行为通过时不为追求红而破坏代码。
- [ ] 实现最小修复。目录有罗马页码、分节重启或复杂对象时走真实 WPS 更新和观察，不用 LibreOffice 页码强造兼容缓存；现有 materializer 明确支持的安全子集保留。
- [ ] 重跑针对性与格式应用/TOC保护回归；对 synthetic DOCX 做结构比较，不做真实全稿渲染。独立审阅并 commit：`fix: preserve bound heading pagination and native toc routing`。

### Task 5: WPS 原生证据与独立验收状态

**Files:** 新增 `scripts/native_acceptance.py`、`schema/native-acceptance-v1.schema.json`、`tests/test_native_acceptance.py`、`references/wps-native-acceptance.md`；修改 current_session 状态投影。

**Interfaces:** `record_native_observation(work_dir: Path, observation: dict) -> dict`；`evaluate_acceptance(work_dir: Path) -> dict`。native observation 固定含 `app`, `input_binding`, `native_object_url`, `native_version`（允许明确 null）, `checks`, `save_close_reopen`, `evidence_refs`, `exported_bytes_binding`（无下载则 null）。

- [ ] 写失败测试：上传 unknown 不得重复创建；URL/版本变更需重新定位；无导出 hash 不能复用输入 hash 冒充编辑后文件；保存重开未完成不能通过；WPS PASS 不得改变 Word release；同一观测重放不新增验收次数。

```python
def test_wps_scope_is_not_word_release(native_fixture):
    result = native_fixture.import_completed_wps_checks(no_export=True)
    assert result["native_checks"]["wps"]["status"] == "pass_scoped"
    assert result["word_release"]["status"] == "not_verified"
    assert result["exported_bytes_binding"] is None
    assert result["submission_ready"] is False
```

- [ ] 运行 `python -m pytest tests/test_native_acceptance.py -q`，确认红。
- [ ] 实现 importer 和检查说明，不写绕过现有浏览器权限的上传器。当前会话用受支持浏览器工具实际操作；记录目录各目标、页码序列、keepNext、关键对象/字体观察、保存关闭重开，保留未查范围。新原生编辑后需要新对象版本证据。
- [ ] 重跑回归及预算测试；独立审阅并 commit：`feat: record scoped wps acceptance without word claims`。

### Task 6: 完整回归、打包、安装及真实会话验证

**Files:** 更新 `SKILL.md`、`README.md`、`docs/install-dot.md`；新增 `docs/validation/current-session-release.md`（仅无隐私摘要）。真实运行证据保留在仓库外。

**Interfaces:** `release-status.json` 独立记录 `source_delivery`、`installation`、`dot_execution`，每项为 passed/failed/blocked/not_run，并附精确版本/证据引用；不设置跨层假“总通过”。

- [ ] 静态核现有 Makefile/pytest 的外部工具路径，配置真实调用守卫和验证账。运行适用完整回归：`make check`、`python -m pytest -q`。历史跳过照实列出；必须执行的集成项缺环境就是 blocker，不假装完整通过。
- [ ] 独立全分支代码审阅，修复发现并复跑受影响测试；校验包解压后资源闭包、路径含空格、只读包目录及可写独立工作区。
- [ ] 核实际支持的 dot skill 安装/加载机制，按官方支持入口安装准确包版本。拿到实际发现/加载证据和运行文件 hash；仅复制 SKILL.md 不算安装。如要求新增持久访问或扩权，等待该次必要审批，设计批准不能替代。若机制不可用，停止安装层并报告最小缺项，不能伪造注册成功。
- [ ] **候选1**：当前 dot 会话从 synthetic requirements+DOCX 开始，完成 packet、merge、内部独立审阅、生成及一次受控全稿渲染；中途在语义任务处暂停并恢复。必要时用此候选测试 WPS，避免无必要私人数据传输。**候选2**仅在需要修复复测时使用新的无隐私合成材料，并核对独立的来源绑定及上传权限范围。私人输入和具体运行来源记录保留在仓库外，不作为可公开测试夹具。每候选不得超两次完整渲染/一次WPS保存重开；缺权限先问，预算耗尽停止。
- [ ] 检查：本次语义与工具执行在当前会话完成；无原工程线程执行依赖、无外部模型CLI；产物和审阅 exact-binding 正确；WPS限定状态与Word发布仍分开。原生流程 unknown 时先查结果，不重发。
- [ ] 代码/文档就绪且适用测试通过后 commit，push 项目分支。提交前剔除私人授权记录、Space句柄、工作区绝对路径及未授权材料，仅保留可公开的设计/计划与技术摘要。核远端 commit 与对应 CI，修 CI 只能在已批准实现范围内；不 force push、不 merge。若安装需先从远端取包，可先 push 已审查测试的候选提交，但最终完成仍等安装/真实运行核证，不把阶段顺序依赖伪称终态。
- [ ] 交付精确 GitHub commit/分支、包版本与 hash、安装/加载证据、实际 dot 用例结果、消费预算、未验边界。Thesis 完成或出现明确待用户技术阻塞后，将精确结果交父协调，不能擅自跳过本项目进入 Academic。

## 自检与停止条件

- 任务1提供包/能力给2；2提供绑定/预算给3、4、5；3/4/5接入同一状态投影；6才使用真实文档/浏览器及完成交付。
- 5项 Review Focus 均有具名失败回归；接口字段在所有任务一致。测试中的 fixture/host evidence 只用于测试，不能生产入账。
- 真实候选生成、完整渲染、浏览器验收前都核预算与授权。没有可用安装或独立身份观察机制时，允许输出审查稿，但不能将缺失关卡宣布完成。
- 本计划不要求这次修复所有学校规则、全部 OLE/数学及 Word Web 兼容问题，不允许以扩大范围替代完成当前目标。
