# LLM 提取规则：从学校论文规范文档提取配置 Overlay

## 任务描述

你是一位学术论文排版规范分析师。给定一份学校的**研究生毕业论文格式规范文档**（PDF/Word/网页），你需要提取其中的排版规则，输出为 YAML 格式的 **配置 overlay 文件**。

该 overlay 文件与 `config-schema-v2.yaml`（通用 schema）配合使用。**只需输出与 schema 默认值不同的字段**，未覆盖的字段将自动使用 schema 默认值。

## 输出格式

```yaml
school:
  name: "学校全称"
  name_en: "英文校名"
  spec_version: "规范文档版本/年份"

metadata:
  # 如果规范文档中有示例值或模板截图，提取为占位值
  author: "张三"                    # 示例作者名（无则留空）
  author_en: "Zhang San"
  advisor: "李四教授"
  advisor_en: "Prof. Li Si"
  school: "金融学院"
  school_en: "School of Finance"
  major: "金融学"
  major_en: "Finance"
  research_direction: "金融工程"
  classification_number: "F830.91"
  confidentiality_level: "公开"
  submit_date: "2026年6月"
  defense_date: "2026年6月"
  title: "论文题目示例"
  title_en: "Sample Thesis Title"
  degree_display: "经济学博士"
  degree_display_en: "Doctor of Economics"

cover:
  enabled: true                     # 是否包含封面
  top_lines:                        # 封面顶部文字（多行）
    - "学校全称"
    - "博士毕业（学位）论文"
  title_style: "Title"
  subtitle_style: "Subtitle"
  subtitle_prefix: "——"
  info_fields:                      # 封面信息表字段
    - label: "专业名称"
    - label: "作者学号"
    - label: "论文作者"
    - label: "指导教师"
  date_style: "CoverDate"

title_page:
  enabled: true
  classification: false             # 是否显示分类号/密级
  degree_line: "博士毕业（学位）论文"
  info_fields:                      # 扉页信息表字段
    - label: "论文作者"
    - label: "指导教师"
  compact_name_labels: ["作者", "导师姓名"]

cn_abstract:
  heading: "摘要"

en_abstract:
  heading: "Abstract"

cn_keywords:
  label: "关键词："
  delimiter: "，"
  min_count: 3
  max_count: 5

en_keywords:
  label: "Key Words:"
  delimiter: "; "
  min_count: 3
  max_count: 5

toc:
  enabled: true
  heading: "目录"
  depth: 3

list_of_figures:
  enabled: true                     # 是否生成插图目录
  heading: "插图目录"

list_of_tables:
  enabled: true                     # 是否生成附表目录
  heading: "附表目录"

sections:
  heading1_format: "第{chapter}章  {title}"     # 注意："章"后通常是两个空格
  heading2_format: "{chapter}.{section}  {title}"
  heading3_format: "{chapter}.{section}.{subsection}  {title}"
  numbering_depth: 3

appendix:
  label_style: "alpha"              # alpha=A/B/C, number=1/2/3, chinese=一/二/三
  heading_format: "附录{label}  {title}"

bibliography:
  heading: "参考文献"
  citation_format: "numeric"
  entry_prefix: "["
  entry_suffix: "]"
  footnote_number_format: "decimalEnclosedCircleChinese"
  min_entries_by_level:
    doctor: 100
    master: 40

research_outputs:
  enabled: false                    # 博士为 true
  heading: "在学期间发表的学术论文与研究成果"

acknowledgements:
  heading: "后记"                   # 或 "致谢"，看学校规范
  signature:
    enabled: true

page:
  size: "A4"
  margins:
    top: 1134                       # twips（见下方单位换算）
    bottom: 850
    left: 1134
    right: 1134
    header_distance: 850
    footer_distance: 992

typography:
  body_font_cn: "SimSun"            # 宋体
  heading_font_cn: "SimHei"         # 黑体
  body_font_en: "Times New Roman"
  heading_font_en: "Times New Roman"
  body_line_spacing:
    value: 20                       # pt 单位（不是 twips）
    unit: pt
  body_line_rule: "exact"           # exact=固定值, atLeast=最小值, auto=自动
  heading_line_spacing:
    value: 20
    unit: pt
  heading_line_rule: "atLeast"
  footnote_line_spacing:
    value: 15
    unit: pt
  footnote_line_rule: "exact"

font_sizes:
  body: 12                          # 正文 12pt = 小四
  heading1: 16                      # 16pt = 三号
  heading2: 14                      # 14pt = 四号
  heading3: 12                      # 12pt = 小四
  abstract_body: 12
  abstract_title: 16
  keywords: 12
  caption: 10.5                     # 10.5pt = 五号
  table_content: 10.5
  source_note: 9
  header_footer: 9
  footnote: 9
  cover_title: 22                   # 22pt = 二号
  cover_info: 14                    # 14pt = 四号

body_text:
  first_line_indent: 480            # 480 twips = 两个中文字符 ≈ 24pt
  first_paragraph_indent: false
  paragraph_spacing_before: 0
  paragraph_spacing_after: 0

figures_tables:
  figure:
    prefix: "图"
    numbering: "chapter-seq"        # chapter-seq=按章编号, global=全文连续
    caption_position: "below"       # below=图下, above=图上
  table:
    prefix: "表"
    numbering: "chapter-seq"
    caption_position: "above"       # above=表上, below=表下
    border_style: "three_line"      # three_line=三线表, all_borders=全框线

equations:
  numbering: "chapter-seq"
  label_format: "（{chapter}-{seq}）"

header_footer:
  different_first_page: true
  different_odd_even: false
  header:
    mode: "chapter_title"           # chapter_title/ text/ none
  footer:
    mode: "page_number"             # page_number/ text
    alignment: "center"
  page_number:
    format: "decimal"
    suppress_first_page: true
    front_matter_format: "roman"
```

## 逐字段提取指南

### 1. school（学校信息）
- `name`：学校规范文档顶部/首页校名
- `name_en`：英文校名（如果规范中有）
- `spec_version`：规范文档的版本号/发布日期/修订年份

### 2. metadata（论文元数据）
- 所有字段提取规范文档中的**示例值**或模板截图中的占位文字
- `degree_display`：从规范中"学位论文"字样推断，如"经济学博士""管理学硕士"
- `classification_number`：如果规范中说明了分类号格式
- `confidentiality_level`：如果规范提及密级管理
- 日期格式：通常为"YYYY年MM月"

### 3. cover（封面）
- `top_lines`：规范文档中封面截图/描述的顶部多行文字
- `info_fields`：封面信息表包含哪些字段（按规范中出现的顺序）
- 注意区分封面 vs 扉页：封面通常更简洁，扉页更详细

### 4. title_page（扉页）
- `degree_line`：扉页顶部的学位类型文字
- `info_fields`：扉页信息表字段（通常比封面更多）
- `classification`：是否在扉页显示分类号和密级

### 5. 摘要 & 关键词
- `cn_abstract.heading`：中文摘要标题（通常为"摘要"或"内容摘要"）
- `cn_keywords.delimiter`：关键词分隔符（中文通常用"，"或"；"）
- `cn_keywords.min/max_count`：关键词数量要求
- `en_keywords.delimiter`：英文关键词分隔符（通常为"; "）

### 6. toc（目录）
- `depth`：目录包含到几级标题（通常为 3 级）
- 如果学校要求生成插图/附表目录，`list_of_figures/list_of_tables.enabled = true`

### 7. sections（章节编号）
- `heading1_format`：一级标题格式
  - 常见："第{chapter}章  {title}"（章后两个空格）
  - 或："{chapter}  {title}"（没有"第"和"章"）
- 注意规范中"章"与标题之间的空格数

### 8. appendix（附录）
- `label_style`：附录编号方式，看规范中写的是"附录A"还是"附录一"
  - alpha → A/B/C
  - chinese → 一/二/三
  - number → 1/2/3

### 9. bibliography（参考文献）
- `citation_format`：引用格式
  - numeric → 编号制 [1][2]
  - author_date → 著者-出版年
- `entry_prefix/suffix`：引用编号的包裹符号（通常 [ 和 ]）
- `min_entries_by_level`：不同学位层次的最低参考文献数量
- `footnote_number_format`：脚注标号样式
  - decimalEnclosedCircleChinese → ①②③
  - decimal → 1,2,3
  - chineseEnclosedCircle → ①,②,③

### 10. page（页面设置）
- 所有 margin 值单位为 **twips**（1 pt = 20 twips，1 cm ≈ 567 twips）
- **单位换算公式**：
  - cm → twips：`cm × 567`
  - inch → twips：`inch × 1440`
  - mm → twips：`mm × 56.7`
  - pt → twips：`pt × 20`
- 如果规范中使用 cm，需要转换

### 11. typography（排版字体）
- `body_font_cn`：中文正文字体（最常见：SimSun 宋体）
- `heading_font_cn`：中文标题字体（最常见：SimHei 黑体）
- `body_font_en`：英文正文字体（最常见：Times New Roman）
- `body_line_spacing`：正文行距
  - **单位是 pt**（不是 twips！），需要转换
  - 如果规范写"20 磅"→ `value: 20, unit: pt`
  - 如果规范写"1.5 倍行距"→ `value: 1.5, unit: multiple`（需在注释中说明）
  - 如果规范写"固定值 20 磅"→ `value: 20, unit: pt, body_line_rule: "exact"`

### 12. font_sizes（字号）
- 所有值单位为 **pt**（1 pt = 1 磅）
- **中文号数 ↔ pt 对照表**：

| 中文号数 | pt 值 |
|---------|-------|
| 初号    | 42    |
| 小初    | 36    |
| 一号    | 26    |
| 小一    | 24    |
| 二号    | 22    |
| 小二    | 18    |
| 三号    | 16    |
| 小三    | 15    |
| 四号    | 14    |
| 小四    | 12    |
| 五号    | 10.5  |
| 小五    | 9     |
| 六号    | 7.5   |

- 如果规范中说"三号"，填 16；"小四"，填 12；"五号"，填 10.5

### 13. body_text（正文段落）
- `first_line_indent`：首行缩进，单位 twips
  - 规范写"两个字符"或"两字"→ 480 twips（12pt 中文两字宽）
  - 规范写"2 字符"→ 480 twips
  - 规范写"不缩进"→ 0
- `first_paragraph_indent`：章节首段是否也缩进（有些学校要求首段不缩进）

### 14. figures_tables（图表）
- `figure.prefix`：图标题前缀
  - 中文论文通常"图"
  - 英文论文通常"Figure"或"Fig."
- `figure.numbering`：图编号方式
  - chapter-seq → 图 1-1, 图 2-3
  - global → 图 1, 图 2
- `figure.caption_position`：图题位置
  - below → 图下（中国高校标准）
  - above → 图上
- `table.numbering`：表编号方式（同上）
- `table.caption_position`：表题位置
  - above → 表上（中国高校标准）
  - below → 表下
- `table.border_style`：表格边框
  - three_line → 三线表（学术论文标准）
  - all_borders → 全框线表
- `caption` 字号通常比正文小（五号=10.5pt）

### 15. equations（公式）
- `numbering`：公式编号方式
  - chapter-seq → （1-1），（2-5）
  - global → （1），（2）
- `label_format`：编号格式
  - 常见：`（{chapter}-{seq}）` 或 `({chapter}.{seq})`
  - 注意中英文括号区别

### 16. header_footer（页眉页脚）
- `different_first_page`：首页是否不同（封面/扉页无页眉，通常是 true）
- `different_odd_even`：奇偶页是否不同（有些学校要求奇偶页不同，多数为 false）
- `header.mode`：
  - chapter_title → 页眉显示当前章节名
  - text → 固定文字（如"XX大学博士学位论文"）
  - none → 无页眉
- `footer.mode`：
  - page_number → 页脚显示页码
  - text → 固定文字
- `page_number.suppress_first_page`：首页是否隐藏页码（通常是 true）
- `page_number.front_matter_format`：前置页码格式（摘要、目录等），通常 roman（I, II, III）

### 17. 字数要求（来自 degree_levels）
- 如果规范中明确要求了正文/摘要字数：
  - 博士：`cn_abstract_min_chars`（中文摘要最少字数）、`body_min_chars`（正文最少字数）
  - 硕士：同上
- 注意：有些学校用"字"，有些用"万字"（需要换算）

## 特殊规则

### 规则 1：脱敏要求
- 输出的 overlay 中**不要包含真实个人信息**
- 使用示例值/占位值（如"张三""2026年6月"）
- 学号用"2023000001"等占位格式

### 规则 2：单位必须显式
- 所有带单位的数值，必须在注释中标明原始值和单位
- 例：页边距 2.5cm → `margins.top: 1418  # 2.5cm → twips`

### 规则 3：多值选择规则
- 如果规范文档中对同一参数给出了多种选项（如"行距 20-22 磅"），取最小值或中间值
- 在注释中标明范围

### 规则 4：缺失参数处理
- 如果规范文档中**没有明确指定**某个参数，不要写入 overlay
- Schema 中的默认值将自动生效

### 规则 5：学位层次覆盖
- 如果博士和硕士有不同的要求，在 `degree_levels` 下分别填写
- 如果规范只涵盖博士，"metadata" 和 `degree_levels.doctor` 同时填写

## 质量检查清单

输出前请自检：
1. ✅ 所有 cm/inch 值已转换为 twips：(cm × 567) 或 (inch × 1440)
2. ✅ 所有中文字号已转换为 pt（参考上表）
3. ✅ 所有行距单位已明确（pt 还是 multiple）
4. ✅ 章节标题格式中的空格数已核对
5. ✅ 封面/扉页信息表字段顺序与规范文档一致
6. ✅ 参考文献引用格式（[ ] vs ( ) ）已核对
7. ✅ 脚注编号样式已核对（①②③ vs 1,2,3）
8. ✅ 图表编号方式已核对（按章 vs 全文）
9. ✅ 页眉页脚模式已核对（章节名 vs 固定文字 vs 无）
10. ✅ 没有夹带真实个人信息（全部使用示例值）

## 输出指令

1. 输出**纯 YAML**（不要用 markdown 代码块包裹）
2. 只输出与 schema 默认值不同的字段
3. 数值字段附注释说明原始单位和换算过程
4. 如果某个字段在规范文档中完全没有提及，不要输出
