# thesis-forge

**V2 通用毕业论文 LaTeX → DOCX 转换管线** — 支持多校配置，一条命令完成从 LaTeX 源文件到符合学校排版规范的 Word 文档的全自动转换。

[![Status](https://img.shields.io/badge/status-beta-orange)](https://github.com/ccmxigua/thesis-forge)
[![Python](https://img.shields.io/badge/python-3.9+-blue)](https://www.python.org/)
[![Pandoc](https://img.shields.io/badge/pandoc-3.1+-green)](https://pandoc.org/)

---

<div align="center">

## 🤖⚠️ 本项目由 AI 生成 ⚠️🤖

**本项目（包括全部代码、配置文件、文档及 README）由 [OpenClaw](https://github.com/openclaw/openclaw) + DeepSeek 大模型辅助生成。**

所有内容均已经过人工审核和测试验证，但如有疑问请以实际运行结果为准。

</div>

---

## 目录

- [项目简介](#项目简介)
- [设计架构](#设计架构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [核心概念](#核心概念)
- [完整使用流程](#完整使用流程)
  - [第一步：准备 Overlay 配置文件](#第一步准备-overlay-配置文件)
  - [第二步：构建参考文档](#第二步构建参考文档)
  - [第三步：运行转换](#第三步运行转换)
  - [第四步：检查输出](#第四步检查输出)
  - [第五步：红队验证（可选）](#第五步红队验证可选)
- [添加新学校](#添加新学校)
- [LLM 辅助提取配置](#llm-辅助提取配置)
- [管线详解](#管线详解)
  - [预处理（preprocess_tex.py）](#预处理preprocess_texpy)
  - [Pandoc 转换](#pandoc-转换)
  - [后处理（postprocess_docx.py）](#后处理postprocess_docxpy)
- [参考文档（reference.docx）](#参考文档referencedocx)
- [红队验证系统](#红队验证系统)
- [常见问题](#常见问题)
- [管线状态](#管线状态)
- [贡献指南](#贡献指南)

---

## 项目简介

写毕业论文时，LaTeX 排版虽然精美，但很多学校要求提交 Word（.docx）格式。手动转换费时费力，而且极难保证格式完全符合学校规范。

**thesis-forge** 解决的就是这个问题：

- 📄 **一条命令转换**：`./convert.sh thesis.tex output.docx overlay.yaml`
- 🏫 **多学校支持**：通过 overlay 配置覆盖学校特有规则，不改核心代码
- 🤖 **LLM 辅助**：提供标准 prompt，用 LLM 从学校规范文档中提取排版参数
- 🔧 **可扩展管线**：预处理 → Pandoc 转换 → 后处理，每步可独立定制
- ✅ **红队验证**：自动对比输出与基线，检测排版回归

本项目的参考实现基于**天津财经大学博士论文规范**，但通过通用 schema 设计，可适配任意高校的论文格式要求。

---

## 设计架构

```
                    ┌─────────────────────────┐
                    │  学校规范文档（PDF/Word）  │
                    └───────────┬─────────────┘
                                │ LLM 提取
                                ▼
┌─────────────────┐    ┌───────────────────────┐
│ config-schema-   │◄───│ config-<school>-       │
│ v2.yaml          │    │ overlay.yaml           │
│ (通用 schema)    │    │ (学校特有配置)          │
└────────┬────────┘    └───────────┬───────────┘
         │                         │
         │         ┌───────────────┘
         │         ▼
         │  ┌──────────────────────┐
         │  │ mapper-v2-to-tjufe.py │
         │  │ (schema → Pandoc meta)│
         │  └──────────┬───────────┘
         │             │
         │             ▼
         │  ┌──────────────────────┐
         │  │   Pandoc metadata     │
         │  └──────────┬───────────┘
         │             │
         ▼             ▼
  ┌─────────────────────────────────────────┐
  │              convert.sh                  │
  │                                         │
  │  ① preprocess_tex.py  ← 预处理 LaTeX    │
  │  ② pandoc + lua filter ← 核心转换       │
  │  ③ postprocess_docx.py ← 后处理 DOCX    │
  └─────────────────────┬───────────────────┘
                        │
                        ▼
               ┌────────────────┐
               │  output.docx   │
               └────────────────┘
```

**设计理念**：

- **单一 Schema，多校 Overlay**：`config-schema-v2.yaml` 定义完整的通用论文配置结构，每个学校只需提供一个轻量 overlay 覆盖差异字段。核心代码（mapper、filter、预处理/后处理脚本）对任何学校通用。
- **LLM 辅助提取**：由 LLM 从学校官方模板规范中提取字段值，经人工审核后形成 overlay。`prompts/extract-config.md` 提供了完整的提取指南。
- **Mapper 桥接**：`mapper-v2-to-tjufe.py` 将 V2 schema 映射为下游 Pandoc 管线可用的 metadata YAML。

---

## 环境要求

| 依赖 | 最低版本 | 安装方式 | 用途 |
|------|---------|---------|------|
| **Python** | 3.9+ | [python.org](https://www.python.org/) | 预处理/后处理/mapper 脚本 |
| **Pandoc** | 3.1+ | `brew install pandoc` 或 [pandoc.org](https://pandoc.org/installing.html) | LaTeX → DOCX 核心转换 |
| **PyYAML** | 6.0+ | `pip install pyyaml` | YAML 配置文件解析 |
| **Git**（可选） | — | `brew install git` | 克隆仓库 |
| **LLM API**（可选） | — | 任意 OpenAI 兼容 API | 用 LLM 自动提取学校配置 |

**快速安装**（macOS）：

```bash
# 安装 Pandoc
brew install pandoc

# 安装 Python 依赖
pip3 install pyyaml
```

---

## 快速开始

### 三步体验

```bash
# 1. 克隆仓库
git clone https://github.com/ccmxigua/thesis-forge.git
cd thesis-forge

# 2. 填写你的个人信息到 overlay 文件
cp schema/config-tjufe-overlay.example.yaml my-config.yaml
# 编辑 my-config.yaml，将 <...> 占位符替换为你的真实信息

# 3. 运行转换
./convert.sh thesis.tex output.docx my-config.yaml
```

转换完成后，`output.docx` 就是符合格式要求的 Word 文档。

### 完整流程（五步）

详见下方 [完整使用流程](#完整使用流程) 章节。

---

## 目录结构

```
thesis-forge/
│
├── convert.sh                              # 🚀 主入口：一键转换脚本
├── validate.sh                             # 🔍 红队验证：对比输出与基线
├── reference.docx                          # 📐 参考样式文档（Pandoc --reference-doc）
│
├── schema/                                 # 配置层
│   ├── config-schema-v2.yaml               #    V2 通用 schema（所有字段定义 + 默认值）
│   └── config-tjufe-overlay.example.yaml   #    天财 overlay 示例（脱敏版）
│
├── scripts/                                # 工具脚本
│   ├── mapper-v2-to-tjufe.py               #    schema overlay → Pandoc metadata 桥接
│   ├── preprocess_tex.py                   #    LaTeX 预处理（交叉引用/公式标记等）
│   ├── postprocess_docx.py                 #    DOCX 后处理（页面/样式/页码修正）
│   ├── build_reference_docx.py             #    从 schema 构建 reference.docx
│   ├── patch_preprocess_generic_markers.py #    通用标记预处理补丁
│   ├── patch_preprocess_bibliography.py    #    参考文献预处理补丁
│   ├── patch_postprocess_labels.py         #    标签后处理补丁
│   ├── patch_postprocess_marker_regex.py   #    标记正则后处理补丁
│   └── ...
│
├── filters/                                # Pandoc Lua 过滤器
│   └── thesis-v2.lua                       #    论文专用 Lua filter
│
├── csl/                                    # 参考文献格式
│   └── china-national-standard-gb-t-7714-2015-numeric.csl  # GB/T 7714-2015
│
├── prompts/                                # LLM prompt 模板
│   └── extract-config.md                   #    从学校规范文档提取 overlay 的 prompt
│
├── tests/                                  # 测试文件
│   └── sample-thesis.tex                   #    样本论文（用于验证）
│
├── redteam/                                # 红队验证系统
│   ├── extract_docx.py                     #    提取 DOCX 内部属性为 JSON
│   ├── compare.py                          #    对比基线 vs 候选输出
│   ├── report.md                           #    验证报告
│   ├── baseline/                           #    基线数据（参考输出）
│   │   ├── styles.json
│   │   ├── paragraphs.json
│   │   ├── numbering.json
│   │   └── page.json
│   └── candidate/                          #    候选数据（当前输出）
│       └── ...
│
└── README.md                               # 本文件
```

---

## 核心概念

### Schema（通用配置模型）

`schema/config-schema-v2.yaml` 定义了一套跨高校的论文排版规范描述语言，涵盖：

| 分类 | 涵盖内容 |
|------|---------|
| **school** | 学校名称、规范版本 |
| **metadata** | 作者、导师、专业、题目等论文元数据 |
| **degree_levels** | 博士/硕士/专硕的摘要字数、正文字数要求 |
| **page** | 纸张大小、页边距（twips） |
| **cover** | 封面文字、信息表字段、样式 |
| **title_page** | 扉页布局、英文标题、信息表 |
| **declarations** | 独创性声明、授权书 |
| **cn_abstract / en_abstract** | 中英文摘要 |
| **cn_keywords / en_keywords** | 关键词格式、分隔符、数量限制 |
| **toc / list_of_figures / list_of_tables** | 目录和图表目录 |
| **sections** | 章节标题格式、编号深度 |
| **appendix** | 附录编号方式 |
| **bibliography** | 参考文献格式、最低数量 |
| **typography** | 中英文字体、行距、字号 |
| **figures_tables** | 图表标题格式、编号、边框 |
| **equations** | 公式编号格式 |
| **header_footer** | 页眉页脚模式、页码格式 |

所有字段都有合理的默认值。Overlay 只需覆盖与默认值不同的字段。

### Overlay（学校特有配置）

Overlay 是学校特异性的轻薄配置文件，只包含**与通用 schema 默认值不同的字段**。

示例（天津财经大学，脱敏版）：
```yaml
school:
  name: "天津财经大学"
  spec_version: "2026.03"

metadata:
  author: "<作者姓名>"

page:
  margins:
    top: 1134     # 2.0cm → twips
    left: 1134    # 2.0cm → twips
```

> ⚠️ 所有单位必须显式标明。长度单位统一使用 **twips**（1 pt = 20 twips，1 cm ≈ 567 twips）。

### Mapper（桥接器）

`mapper-v2-to-tjufe.py` 读取 overlay YAML，映射为 Pandoc 和 Lua filter 可识别的 metadata YAML。不同学校可能需要不同的 mapper，但映射逻辑类似。

### Pipeline（转换管线）

`convert.sh` 串联以下步骤：

1. **预处理**：处理 LaTeX 交叉引用、公式标签、参考文献引用等 Pandoc 不能完美处理的结构
2. **Pandoc 转换**：使用 Lua filter + reference docx + metadata 将 TeX 转为 DOCX
3. **后处理**：修正 DOCX 的页面设置、样式继承、页码格式、图表题注等

---

## 完整使用流程

### 第一步：准备 Overlay 配置文件

有两种方式获得 overlay 文件：

#### 方式 A：人工编写（适合已有天财模板）

```bash
# 复制示例文件
cp schema/config-tjufe-overlay.example.yaml my-thesis-config.yaml

# 用编辑器打开，替换所有 <...> 占位符
# 需要填写的内容包括：
#   - 个人信息：姓名、学号、专业、导师等
#   - 论文信息：题目、提交日期、答辩日期等
#   - 学校信息：学院名称等
vim my-thesis-config.yaml
```

#### 方式 B：LLM 提取（适合新学校）

详见 [LLM 辅助提取配置](#llm-辅助提取配置) 章节。

### 第二步：构建参考文档

`reference.docx` 是 Pandoc 的 `--reference-doc` 参数使用的样式模板。首次使用或修改 schema 后需重新构建：

```bash
python3 scripts/build_reference_docx.py
```

这会根据 `config-schema-v2.yaml` 中的样式定义生成 `reference.docx`。如果仓库中已有预构建的 `reference.docx`，此步可跳过。

### 第三步：运行转换

```bash
# 基本用法
./convert.sh <input.tex> <output.docx> [overlay.yaml] [extra pandoc args...]

# 示例：使用天财 overlay 转换
./convert.sh my-thesis.tex my-thesis.docx schema/config-tjufe-overlay.example.yaml

# 不使用 overlay（使用 schema 默认值）
./convert.sh my-thesis.tex my-thesis.docx

# 传递额外的 Pandoc 参数
./convert.sh my-thesis.tex my-thesis.docx my-config.yaml --bibliography=refs.bib
```

**脚本做了什么**：

1. 运行 mapper 从 overlay + schema 生成 Pandoc metadata
2. 检查并构建 reference.docx（如需要）
3. 运行 preprocess_tex.py 预处理 LaTeX
4. 运行 pandoc 进行转换（with Lua filter + metadata + reference docx）
5. 运行 postprocess_docx.py 后处理输出

### 第四步：检查输出

用 Word 打开 `output.docx`，检查：

- [ ] 封面信息是否正确
- [ ] 扉页布局是否对齐
- [ ] 目录页码是否正确
- [ ] 中英文摘要格式是否规范
- [ ] 章节标题编号是否正确
- [ ] 图表题注格式是否正确（图下、表上）
- [ ] 参考文献格式是否符合 GB/T 7714-2015
- [ ] 页眉页脚是否显示正确
- [ ] 公式编号是否正确

### 第五步：红队验证（可选）

```bash
./validate.sh <config-overlay.yaml>
```

此脚本会：
1. 用你的 overlay 转换 `tests/sample-thesis.tex`
2. 提取输出 DOCX 的内部属性（样式、段落、编号、页面）
3. 与基线对比，生成合规报告

结果示例：
```
✅ Validation PASSED — no CRITICAL or HIGH findings.
```

---

## 添加新学校

假设要为「XX 大学」添加配置支持：

### 1. 获取学校排版规范文档

找到该校研究生院发布的**毕业论文格式规范**（通常为 PDF 或 Word 文档）。

### 2. 用 LLM 生成 Overlay

使用 `prompts/extract-config.md` 中的 prompt，配合你的 LLM 工具：

```bash
# 将规范文档内容 + prompts/extract-config.md 一起发给 LLM
# LLM 会输出一个 YAML overlay 文件
```

你也可以手动编写 overlay：

```bash
cp schema/config-tjufe-overlay.example.yaml config-xxu-overlay.yaml
# 根据 XX 大学的规范修改所有字段
```

### 3. 填写关键参数

至少需要确认以下参数：

| 参数 | 说明 | 常见取值 |
|------|------|---------|
| `page.margins` | 页边距（cm→twips） | 天财：上 2.0/下 1.5/左 2.0/右 2.0 cm |
| `typography.body_font_cn` | 中文正文字体 | 宋体 SimSun |
| `typography.body_line_spacing` | 正文行距 | 固定值 20pt |
| `font_sizes.body` | 正文字号 | 小四 = 12pt |
| `sections.heading1_format` | 一级标题格式 | `第{chapter}章  {title}` |
| `figures_tables.figure.caption_position` | 图题位置 | below（图下） |
| `figures_tables.table.caption_position` | 表题位置 | above（表上） |
| `bibliography.citation_format` | 引用格式 | numeric |
| `cover.top_lines` | 封面顶部文字 | `["XX大学", "博士毕业（学位）论文"]` |

### 4. 测试

```bash
# 使用样本论文测试
./convert.sh tests/sample-thesis.tex test-output.docx config-xxu-overlay.yaml

# 用 Word 打开检查
open test-output.docx

# 运行红队验证
./validate.sh config-xxu-overlay.yaml
```

### 5. （可选）编写新的 Mapper

如果 overlay 结构与天财差异较大，可复制并修改 mapper：

```bash
cp scripts/mapper-v2-to-tjufe.py scripts/mapper-v2-to-xxu.py
# 根据 XX 大学的字段映射关系修改
# 然后在 convert.sh 中切换 mapper
```

---

## LLM 辅助提取配置

`prompts/extract-config.md` 包含一个完整的、逐字段说明的 LLM prompt，用于从学校规范文档中提取排版参数。

### 使用方法

1. 获得学校规范文档的**纯文本内容**（PDF 可用 OCR/转换工具，Word 可直接复制）
2. 将 `prompts/extract-config.md` 的内容 + 规范文档文本一起发送给 LLM
3. LLM 会输出一个 YAML 格式的 overlay 文件
4. 人工审核输出，核对：
   - 单位换算是否正确（cm → twips）
   - 字号转换是否正确（号数 → pt）
   - 空格数是否正确（如 `"第{chapter}章  {title}"` 中"章"后的两个空格）
5. 保存为 `config-<school>-overlay.yaml`

### Prompt 涵盖的内容

- **17 个字段分类**：从 school 基本信息到 header_footer 页眉页脚
- **单位换算表**：cm/inch/mm/pt → twips；中文号数 → pt
- **4 条特殊规则**：脱敏、单位显式、多值选择、缺失参数处理
- **10 项质量检查清单**：输出前的自检项目

---

## 管线详解

### 预处理（preprocess_tex.py）

LaTeX 有许多 Pandoc 不能完美处理的结构，预处理脚本负责：

1. **交叉引用标记**：将 `\ref{fig:xxx}` / `\autoref{tab:xxx}` 转换为 `[[[TJUFE_XREF:label|text]]]` 标记，供 Pandoc 透传
2. **公式编号标记**：为 `equation`/`align` 等环境插入 `[[[TJUFE_EQLABEL:xxx]]]` 标记
3. **\label 处理**：将 `\label{xxx}` 转换为 `TJUFE_LABEL__xxx__` 透传标记
4. **参考文献预处理**：处理 BibTeX 引用，支持 `\cite`/`\citep`/`\citet` 等变体
5. **辅助文件解析**：读取 `.aux` 文件获取交叉引用信息

这些标记在 DOCX 后处理阶段被还原为标准格式。

### Pandoc 转换

```bash
pandoc preprocessed.tex \
  --from=latex+raw_tex \
  --to=docx \
  --standalone \
  --reference-doc=reference.docx \
  --lua-filter=filters/thesis-v2.lua \
  --metadata-file=<mapper-output>.yaml \
  -o raw.docx
```

- **`--reference-doc`**：提供样式模板（字体、字号、间距等）
- **`--lua-filter`**：`thesis-v2.lua` 处理 Pandoc AST，负责章节编号、图表标题前缀、中英文摘要布局等
- **`--metadata-file`**：传递 mapper 生成的 metadata

### 后处理（postprocess_docx.py）

DOCX 本质是 ZIP 包，后处理脚本直接操作 XML：

1. **页面设置**：修正 sectPr 中的页面尺寸、页边距
2. **样式修正**：修正 Pandoc 生成的样式继承关系
3. **页码格式**：设置前置页码（罗马数字）和正文页码样式
4. **图表题注**：从透传标记还原交叉引用
5. **脚注格式**：设置脚注编号样式（如 ①②③）
6. **表格边框**：设置三线表样式
7. **目录占位**：插入 TOC 域代码

---

## 参考文档（reference.docx）

`reference.docx` 是 Pandoc 样式参考的核心。它定义了 Word 文档中所有可用样式的外观：

- 标题样式（Heading 1/2/3）
- 正文样式（Normal、Body Text）
- 封面/扉页专用样式（CoverDate、TitlePageDegree 等）
- 图表题注样式（Image Caption、Table Caption）

**构建方式**：

```bash
python3 scripts/build_reference_docx.py
```

此脚本从 `config-schema-v2.yaml` 读取样式定义（字体、字号、行距等），生成对应的 `reference.docx`。

**何时需要重新构建**：
- 修改了 schema 中的字体/字号/行距设置
- 添加了新的自定义样式
- 首次克隆仓库（如果预构建的 reference.docx 不可用）

---

## 红队验证系统

`validate.sh` 实现了一套**自动化排版合规验证**：

```
validate.sh overlay.yaml
    │
    ├── [0/3] 提取基线 reference.docx 属性
    │
    ├── [1/3] convert.sh → 生成候选 DOCX
    │
    ├── [2/3] extract_docx.py → 提取候选 DOCX 属性
    │
    └── [3/3] compare.py → 对比基线 vs 候选
            │
            ├── 页面：宽度、高度、页边距
            ├── 样式：样式列表、基本属性
            ├── 段落：段落数量、样式分布
            ├── 编号：编号定义
            └── → report.md
```

**严重级别**：
- **CRITICAL**：页面尺寸不一致（A4 vs 非标）
- **HIGH**：页边距偏差
- **MEDIUM**：段落数差异
- **LOW**：可忽略的微小差异

**使用方法**：

```bash
# 首次运行：建立 baseline（只需一次）
./validate.sh config-tjufe-overlay.example.yaml

# 修改代码后重新验证
./validate.sh config-tjufe-overlay.example.yaml
```

如果验证失败，检查 `redteam/report.md` 了解具体差异。

---

## 常见问题

<details>
<summary><b>Q: 转换后中文变成乱码/方框？</b></summary>

检查 `reference.docx` 中是否正确设置了中文字体（如 SimSun 宋体）。重新运行 `python3 scripts/build_reference_docx.py` 构建参考文档。
</details>

<details>
<summary><b>Q: 页码不连续或格式错误？</b></summary>

这是 Pandoc 的已知限制。后处理脚本会自动修正分节符和页码格式。如果仍有问题，在 Word 中手动检查「布局 → 分隔符 → 下一页」，确保前置部分和正文之间有正确的分节符。
</details>

<details>
<summary><b>Q: 参考文献格式不符合 GB/T 7714-2015？</b></summary>

项目使用 `csl/china-national-standard-gb-t-7714-2015-numeric.csl`。确认你的 `.bib` 文件中条目信息完整。如果缺少必要字段（如 `language`、`location`），CSL 可能无法正确渲染。
</details>

<details>
<summary><b>Q: 公式编号变成了 (1) 而不是 (1-1)？</b></summary>

检查 overlay 中 `equations.numbering` 是否设为 `chapter-seq`，以及 `equations.label_format` 是否为 `（{chapter}-{seq}）`。
</details>

<details>
<summary><b>Q: 图/表的题注跑到了错误的位置？</b></summary>

中国高校标准：图题在图**下方**（`figure.caption_position: "below"`），表题在表**上方**（`table.caption_position: "above"`）。检查你的 overlay 配置是否正确。
</details>

<details>
<summary><b>Q: 如何调试管线中的某一步？</b></summary>

```bash
# 只运行预处理，查看输出
python3 scripts/preprocess_tex.py input.tex preprocessed.tex

# 只运行 Pandoc（不经过 convert.sh）
python3 scripts/mapper-v2-to-tjufe.py my-config.yaml metadata.yaml
pandoc preprocessed.tex --from=latex+raw_tex --to=docx \
  --reference-doc=reference.docx \
  --lua-filter=filters/thesis-v2.lua \
  --metadata-file=metadata.yaml \
  -o raw.docx

# 只运行后处理
python3 scripts/postprocess_docx.py raw.docx final.docx
```
</details>

<details>
<summary><b>Q: 我不是天财的学生，能用吗？</b></summary>

完全可以。天财只是参考实现。按照 [添加新学校](#添加新学校) 的步骤，创建你学校的 overlay 即可。核心管线（preprocess → pandoc → postprocess）对所有学校通用。
</details>

---

## 管线状态

| 模块 | 状态 | 说明 |
|------|------|------|
| V2 通用 Schema | ✅ 完成 | `config-schema-v2.yaml` |
| 天财 Overlay | ✅ 完成 | 脱敏示例版 |
| Mapper 桥接 | ✅ 完成 | `mapper-v2-to-tjufe.py` |
| LaTeX 预处理 | ✅ 完成 | 交叉引用、公式、参考文献处理 |
| Pandoc Lua Filter | ✅ 完成 | `thesis-v2.lua` |
| DOCX 后处理 | ✅ 完成 | 页面、样式、页码修正 |
| 参考文档构建 | ✅ 完成 | `build_reference_docx.py` |
| 红队验证 | ✅ 完成 | 基线对比 + 合规报告 |
| LLM Prompt | ✅ 完成 | `prompts/extract-config.md` |
| GB/T 7714 CSL | ✅ 完成 | 中文参考文献标准格式 |
| 集成测试 | ⏳ 开发中 | 多校样本测试 |
| 更多学校 Overlay | ⏳ 待社区贡献 | 欢迎 PR |

---

## 贡献指南

欢迎贡献！以下是几种参与方式：

### 添加你的学校

1. Fork 本仓库
2. 按照 [添加新学校](#添加新学校) 创建 overlay 文件
3. 使用 `tests/sample-thesis.tex` 测试通过
4. 提交 PR（包含脱敏后的 overlay + 更新的 README）

### 改进管线

- **过滤器增强**：`filters/thesis-v2.lua` 欢迎更多 LaTeX 结构的支持
- **后处理完善**：`scripts/postprocess_docx.py` 欢迎更多格式修正
- **新 Mapper**：如果你的学校需要不同的 metadata 映射，欢迎贡献新的 mapper

### 报告问题

在 [Issues](https://github.com/ccmxigua/thesis-forge/issues) 中提交：
- 你的学校名称和规范文档来源
- 遇到的具体问题（最好附上截图）
- 使用的 overlay 配置（脱敏后）
- 日志输出

---

## 许可证

本项目仅供学术用途。各学校的排版规范版权归相应学校所有。
