# 通用 OMML 公式排版修复的来源与边界

本提交在 `a0f3f59113fdd725fb7ff431df1ac720c135a227` 上重新整理公式相关代码。
它是基于原始源码实物审计后的最小移植，不声称精确还原无法隔离的历史整轮增量。

来源是 Library 文件 `libfile_ac40859cc7fc8191a1eddb8df56f691f`，
`v007_corrected.zip`，1,233,762 字节，SHA-256：
`088044ac7158a0ca3c94a4a062d47e7af40c5eaaa015124aa2714e29af8e8639`。
本次在独立工作区重新下载并核对字节与哈希，读取了包内源码、补丁和测试。
完整工作树补丁的 SHA-256：
`5f73e7805878827ffa30dfc2795088fd17ebbf1610183d01bf618d5cfcfb64fb`，
包含 92 个文件的混合历史改动，没有整体应用。
v006 已含居中和 inline 保护；v006→v007 补丁只代表表格宽度修复。

实现仅涉及 `scripts/docx_semantics.py` 的显示公式识别，以及
`scripts/apply_format_spec.py` 的公式布局、宽度解析和相应序列化审计。
其余改动是定向测试、两个既有公式集成测试及本文档。

- 从 v007 实际源码提取正文/单元格宽度、网格轨道、边距继承和默认缩进处理。
  固定布局按 `tblGrid`、`gridSpan`、`gridBefore` 计算；不采用陈旧的 `tcW`。
  减去有效单元格边距后再放置居中和右编号制表位。
- 显示公式默认使用公式外部的真实 Word 制表符居中，段落保持左锚定。
  不修改 OMML 子树，因此多行公式的内部 `m:jc`、`m:aln` 及数学内容保持原样。
  已声明的公式角色段落属性优先于默认值；显式公式居中配置优先于角色对齐。
- 正文中的行内、混合内容及多个独立数学对象不套用显示公式居中。
  布局执行和审计均遍历表格与嵌套表格。
- 将原包分开的居中/编号空白转换函数整理为一个经过完整结构预检的实现；
  只改公式外部的分隔空白，保留编号分段、字符格式和书签/批注范围标记。
  增加继承制表位截获检查，以及负边距、负 `gridBefore` 的拒绝处理。
- 未移植原包的 OLE/MathType 支持、公式字体扩展、语义规则/Schema 扩展、
  模板识别扩展、附录修复及其他历史工作树改动。
  公式布局仍使用当前仓库既有配置结构。

自适应表格的网格和首选宽度冲突、固定网格无法解析、百分比边距、
超出容器的显式制表位、域结构或不支持的编号会产生审计失败，不能据此宣称排版通过。
当前编号支持带括号的数字及点/连字符分隔的数字；`(T.1)` 等标签保留原样并报告未决。
旧集成测试中超出默认纸张文本区的显式制表位，其夹具纸张已按原包改为足够宽；
另一个测试更新为先验证原始输入缺少编号制表符，再验证执行器修复后的输出。

使用原包 `v007-input.docx` 和原包公式配置在本提交重新生成的小样，
与原包 `v007-output.docx` **逐字节一致**，SHA-256：
`2cdca70a0cb15ff4e898c4af98814c7e4e9884f8db7e679754a257ef401aac95`。
重新检查的 5 个顶层数学子树及行内公式所在段落的规范化 XML 均保持一致；
结构数仍为 `oMathPara=4`、`oMath=5`、`eqArr=2`、`aln=4`。
重新生成收据 `valid=true`、`submission_ready=false`。

本次实际渲染使用 Linux 环境的 LibreOfficeDev 26.8.0.0.alpha0
`2c87e51eeaa2b413ff4ae097b2705eea1995d8e5`；人工查看了新渲染的单页 PNG。
表格内“（3.2）”在新 PDF 中出现一次，字框为
`x=207.7–246.7 pt, y=532.53–549.906 pt`；对应右制表位为 3384 twips。
这只支持该合成小样的 LibreOffice 效果。Word/WPS 原生效果、超长公式溢出和
公式与编号碰撞未验证，不作发布验收或论文内容合规结论。

测试使用 `requirements-test.txt` 的固定 Python 依赖和与 CI 相同的 Pandoc 3.7.0.2。
定向回归命令：

```sh
python3 -m pytest -q tests/test_equation_layout.py tests/test_requirements_pipeline.py -k 'equation or math or formula'
make check
make pytest
```

本次结果：定向 19 passed、141 deselected、12 个子测试通过；
`make check` 编译和 diff 检查通过，单元测试 Ran 1797、OK（6 skipped）；
`make pytest` 为 2222 passed、6 skipped、5 warnings。
跳过项均缺少既有学校/历史集成夹具；警告来自既有转义字符串、Pillow API 和
测试刻意构造的重复 ZIP 项。上述结果来自本提交源码的实际运行，不引用原包测试计数。
