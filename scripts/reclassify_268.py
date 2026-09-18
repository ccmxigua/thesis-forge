#!/usr/bin/env python3
"""
Reclassify unresolved/unsupported_backend items in dlut-response.json
based on semantic analysis of clause text/source_text_full/context.
"""
import json
import os
import sys
import copy
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESPONSE_PATH = os.path.join(BASE, 'build/ten-school-current-20260721/rebuilt-reviews/dlut-response.json')
CLAUSES_PATH = os.path.join(BASE, 'build/ten-school-current-20260721/dlut/work/requirements/requirement-clauses.json')
AUDIT_PATH = os.path.join(BASE, 'build/ten-school-current-20260721/rebuilt-reviews/dlut-semantic-review-audit.json')

# Valid contract 2.1 classifications
VALID_CLASSES = {
    'informational', 'unresolved', 'unsupported_backend', 'unverifiable',
    'requires_metadata', 'requires_source_content', 'external_compliance',
    'executable'
}

# Load data
with open(RESPONSE_PATH) as f:
    response = json.load(f)
with open(CLAUSES_PATH) as f:
    clauses = json.load(f)

clause_map = {c['id']: c for c in clauses}
reviews = response['clause_reviews']
review_map = {r['clause_id']: r for r in reviews}

# Check all clause IDs match
response_ids = set(r['clause_id'] for r in reviews)
clause_ids = set(c['id'] for c in clauses)
assert response_ids == clause_ids, f"ID mismatch: {response_ids - clause_ids}, {clause_ids - response_ids}"
assert len(reviews) == len(clauses) == 1186

# Collect target items
targets = [r for r in reviews if r['classification'] in ('unresolved', 'unsupported_backend')]
print(f"Target items to reclassify: {len(targets)}")

before_counts = Counter(r['classification'] for r in reviews)
print(f"Before counts: {dict(before_counts)}")

changes = []

def classify_clause(clause_id, review):
    """Determine the correct classification for a clause."""
    c = clause_map.get(clause_id, {})
    text = c.get('text', '')
    full = c.get('source_text_full', '')
    ctx_before = c.get('context_before', [])
    ctx_after = c.get('context_after', [])
    all_text = full + ' ' + ' '.join(ctx_before) + ' ' + ' '.join(ctx_after)
    text_lower = text.lower()
    full_lower = full.lower()
    all_lower = all_text.lower()

    # ============================================================
    # Rule 1: "阅后删除此文本框" / "阅后删除此文本框" — template instructions
    # ============================================================
    if '阅后删除此文本框' in text or '阅后删除' in text:
        return 'informational', '模板操作提示"阅后删除此文本框"为说明性文字，指导用户手动删除模板中的占位文本框，非可执行格式要求。'

    # ============================================================
    # Rule 2: "示例" placeholder text
    # ============================================================
    if text.strip() == '示例' or '示例阅后删除此文本框' in full:
        return 'informational', '模板占位示例文字，用于展示格式样例，非可执行要求。'

    # ============================================================
    # Rule 3: "注：在该页面中点击鼠标右键，选择"更新域…" — TOC update instructions
    # ============================================================
    if '更新域' in text or '更新整个目录' in text or '点击鼠标右键' in text:
        return 'informational', '操作指引说明如何手动更新目录域，属于模板使用说明，非格式要求。'

    # ============================================================
    # Rule 4: "请保证该页为奇数页" / "请保证此页为奇数页" — odd page requirement
    # These are real formatting requirements for binding
    # ============================================================
    if '请保证该页为奇数页' in text or '请保证此页为奇数页' in text or text.strip() == '请保证此页为奇数页':
        return 'informational', '模板提示用户确保目录/符号表位于奇数页，属于排版操作说明，非可执行格式要求。'

    # ============================================================
    # Rule 5: "使用鼠标选择相应的样式" — style usage instructions
    # ============================================================
    if '使用鼠标选择相应的样式' in text:
        return 'informational', '模板使用说明，指导用户如何应用样式，非格式要求。'

    # ============================================================
    # Rule 6: "正文"模块后鼠标右键选择"修改"" — style modification instructions
    # ============================================================
    if '鼠标右键选择' in text or '正文' in text and '模块' in text and '修改' in text:
        return 'informational', '模板操作指导，说明如何修改Word样式，属于使用说明。'

    # ============================================================
    # Rule 7: "正文第一页为奇数页注意：通过插入分节符" — odd page instruction
    # ============================================================
    if '正文第一页为奇数页' in text:
        return 'informational', '模板提示通过分节符确保正文从奇数页开始，属于排版操作说明。'

    # ============================================================
    # Rule 8: "注意：请保证此页为奇数页"
    # ============================================================
    if '注意：请保证此页为奇数页' in text:
        return 'informational', '模板提示确保该页为奇数页，属于排版操作说明。'

    # ============================================================
    # Rule 9: Pure section headings / TOC entries (e.g., "2.1 论文文字与格式基本要求2")
    # These are table of contents entries or section headings
    # ============================================================
    if re.match(r'^[\d.]+\s+\S', text) and len(text) < 60:
        return 'informational', '目录条目或章节标题文字，属于文档结构标识，非格式要求。'

    # ============================================================
    # Rule 10: "图 2.4 文本样式设置" — figure caption / TOC entry
    # ============================================================
    if re.match(r'^图\s+[\d.]+', text) or re.match(r'^表\s+[\d.]+', text):
        return 'informational', '图/表目录条目或题注，属于文档结构标识，非格式要求。'

    # ============================================================
    # Rule 11: "论文文字与格式基本要求" — section heading
    # ============================================================
    if text in ('论文文字与格式基本要求', '论文文字要求', '论文格式基本要求',
                '量和单位的使用', '使用方法', '论文文字要求：'):
        return 'informational', '章节标题文字，属于文档结构标识，非格式要求。'

    # ============================================================
    # Rule 12: "若因声明不实，本人愿意为此承担相应的法律责任" — legal statement
    # ============================================================
    if '若因声明不实' in text or '本人愿意为此承担' in text:
        return 'external_compliance', '学位论文原创性声明中的法律责任条款，属于法律合规性声明，非格式要求，需物理签字确认。'

    # ============================================================
    # Rule 13: "大连理工大学学位论文版权使用授权书" — copyright page title
    # ============================================================
    if '大连理工大学学位论文版权使用授权书' in text:
        return 'external_compliance', '版权使用授权书标题，属于法律合规性文件，需物理签字确认。'

    # ============================================================
    # Rule 14: "除正文、目录外，其他部分偶数页为空白页" — blank page note
    # ============================================================
    if '除正文、目录外，其他部分偶数页为空白页' in text:
        return 'informational', '模板说明文字，解释空白页排版规则，属于使用说明。'

    # ============================================================
    # Rule 15: "中文题目，居中，华文细黑，加黑，二号" — formatting requirements for title
    # These are real formatting requirements for the thesis title
    # ============================================================
    if '中文题目' in text and ('居中' in text or '华文细黑' in text or '二号' in text):
        return 'requires_source_content', '论文中文题目格式要求（字体、字号、对齐、行距），属于论文内容格式，需由用户提供题目内容后应用。'

    # ============================================================
    # Rule 16: "中文题目对应，居中，Times New Roman" — English title formatting
    # ============================================================
    if '中文题目对应' in text and ('Times New Roman' in text or '三号' in text):
        return 'requires_source_content', '论文英文题目格式要求（字体、字号、对齐），属于论文内容格式，需由用户提供英文题目内容后应用。'

    # ============================================================
    # Rule 17: "英文标题设置及排版方法" — template version note
    # ============================================================
    if '英文标题设置及排版方法' in text or '与上一版模板相比' in text:
        return 'informational', '模板版本说明文字，介绍模板改进内容，非格式要求。'

    # ============================================================
    # Rule 18: "摘要的主要内容为" / "文中不允许出现插图" — abstract content rules
    # ============================================================
    if '摘要的主要内容' in text or ('文中不允许出现插图' in text):
        return 'requires_source_content', '摘要内容撰写要求，属于论文内容规范，需用户自行撰写摘要内容。'

    # ============================================================
    # Rule 19: "篇幅以一页为限" — page limit
    # ============================================================
    if '篇幅以一页为限' in text:
        return 'unverifiable', '篇幅限制要求（一页为限），属于主观/定性标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 20: "标题"目录"：黑体，小三" — TOC formatting
    # ============================================================
    if '标题' in text and ('目录' in text or 'Table of contents' in text) and ('黑体' in text or '小三' in text):
        return 'informational', '目录标题格式说明，属于模板预设格式说明，非独立可执行要求。'

    # ============================================================
    # Rule 21: "标题"图目录"" / "标题"表目录"" — figure/table list formatting
    # ============================================================
    if '图目录' in text or '表目录' in text:
        if '黑体' in text or '小三' in text:
            return 'informational', '图/表目录标题格式说明，属于模板预设格式说明。'
        return 'informational', '图/表目录标题，属于文档结构标识。'

    # ============================================================
    # Rule 22: "表格内变量字母采用斜体" — table formatting
    # ============================================================
    if '表格内变量字母采用斜体' in text:
        return 'requires_source_content', '表格内容格式要求（变量字母斜体），属于论文内容格式，需用户提供表格内容后应用。'

    # ============================================================
    # Rule 23: "表格的形式采用如下三线表" — three-line table format
    # ============================================================
    if '表格的形式采用如下三线表' in text:
        return 'informational', '三线表格式说明，属于模板预设样式说明，已在模板样式中预设。'

    # ============================================================
    # Rule 24: "正文选用"正文"样式" — body text style
    # ============================================================
    if '正文选用' in text and '正文' in text and '样式' in text:
        return 'requires_source_content', '正文样式格式要求（首行缩进、字体、字号、行距），属于论文内容格式，需用户提供正文内容后应用。'

    # ============================================================
    # Rule 25: "(1) 每章的章标题选用"标题1"样式" — chapter heading style
    # ============================================================
    if '章标题选用' in text and '标题1' in text:
        return 'requires_source_content', '章标题格式要求（标题1样式），属于论文内容格式，需用户提供章节内容后应用。'

    # ============================================================
    # Rule 26: "每章另起一页" — chapter page break
    # ============================================================
    if '每章另起一页' in text and '分页符' in text:
        return 'requires_source_content', '每章另起一页的要求，属于文档结构要求，需在用户提供章节内容后通过分页符实现。'

    # ============================================================
    # Rule 27: "公式一律采用阿拉伯数字分章编号" — formula numbering
    # ============================================================
    if '公式一律采用阿拉伯数字分章编号' in text:
        return 'requires_source_content', '公式编号规范要求，属于论文内容格式，需用户提供公式内容后应用。'

    # ============================================================
    # Rule 28: "图的格式请使用"题注样式"" — figure caption style
    # ============================================================
    if '图的格式请使用' in text and '题注样式' in text:
        return 'requires_source_content', '图题注格式要求，属于论文内容格式，需用户提供图片及题注后应用。'

    # ============================================================
    # Rule 29: "图与下文之间应留一空行" — spacing after figure
    # ============================================================
    if '图与下文之间应留一空行' in text:
        return 'requires_source_content', '图与下文间距要求，属于论文内容格式，需用户提供图片内容后应用。'

    # ============================================================
    # Rule 30: "图中若有附注" — figure notes
    # ============================================================
    if '图中若有附注' in text:
        return 'requires_source_content', '图附注格式要求，属于论文内容格式，需用户提供图片附注后应用。'

    # ============================================================
    # Rule 31: "设置图片格式"的"版式"为"上下型"或"嵌入型"" — image layout
    # ============================================================
    if '设置图片格式' in text and ('上下型' in text or '嵌入型' in text):
        return 'requires_source_content', '图片版式要求（上下型/嵌入型），属于论文内容格式，需用户提供图片后应用。'

    # ============================================================
    # Rule 32: "图名居中并位于图下" — figure caption position
    # ============================================================
    if '图名居中并位于图下' in text:
        return 'requires_source_content', '图名位置要求，属于论文内容格式，需用户提供图片及图名后应用。'

    # ============================================================
    # Rule 33: "图名使用"题注样式"" — figure caption style detail
    # ============================================================
    if '图名使用' in text and '题注样式' in text:
        return 'requires_source_content', '图名字体字号要求，属于论文内容格式，需用户提供图片及图名后应用。'

    # ============================================================
    # Rule 34: "表在正文中的常用格式" / "使用三线表" — table format
    # ============================================================
    if '使用三线表' in text or ('表在正文中的常用格式' in text):
        return 'requires_source_content', '表格格式要求（三线表），属于论文内容格式，需用户提供表格内容后应用。'

    # ============================================================
    # Rule 35: Thesis title content ("基于柔性自适应"热开关"的新型热控方案")
    # ============================================================
    if '基于柔性自适应' in text or '被动热控设计' in text:
        return 'requires_source_content', '论文标题/摘要内容，属于论文内容，需用户提供最终内容。'

    # ============================================================
    # Rule 36: "按照"表格样式"统一" — table style
    # ============================================================
    if '按照' in text and '表格样式' in text:
        return 'requires_source_content', '表格样式要求（居中、行距、缩进），属于论文内容格式，需用户提供表格内容后应用。'

    # ============================================================
    # Rule 37: "表内文字全文统一" — table text font
    # ============================================================
    if '表内文字全文统一' in text:
        return 'requires_source_content', '表内文字字体要求，属于论文内容格式，需用户提供表格内容后应用。'

    # ============================================================
    # Rule 38: "表格与上文应留一行空格" — spacing before table
    # ============================================================
    if '表格与上文应留一行空格' in text:
        return 'requires_source_content', '表格与上文间距要求，属于论文内容格式，需用户提供表格内容后应用。'

    # ============================================================
    # Rule 39: "表中若有附注" — table notes
    # ============================================================
    if '表中若有附注' in text:
        return 'requires_source_content', '表附注格式要求，属于论文内容格式，需用户提供表格附注后应用。'

    # ============================================================
    # Rule 40: "表头请使用"题注样式"" — table caption style
    # ============================================================
    if '表头请使用' in text and '题注样式' in text:
        return 'requires_source_content', '表头题注格式要求，属于论文内容格式，需用户提供表格及表头后应用。'

    # ============================================================
    # Rule 41: "公式行使用"公式样式"" — formula style
    # ============================================================
    if '公式行使用' in text and '公式样式' in text:
        return 'requires_source_content', '公式样式要求（制表位、题注），属于论文内容格式，需用户提供公式内容后应用。'

    # ============================================================
    # Rule 42: "使用"公式样式"" — formula style usage
    # ============================================================
    if '使用' in text and '公式样式' in text:
        return 'requires_source_content', '公式样式使用说明，属于论文内容格式，需用户提供公式内容后应用。'

    # ============================================================
    # Rule 43: "公式序号应按章编号" — formula numbering
    # ============================================================
    if '公式序号应按章编号' in text:
        return 'requires_source_content', '公式序号规范要求，属于论文内容格式，需用户提供公式内容后应用。'

    # ============================================================
    # Rule 44: "国内有人认为" / "也有人认出为" — sample content text
    # ============================================================
    if '国内有人认为' in text or '也有人认出为' in text:
        return 'requires_source_content', '论文正文示例内容，属于论文内容，需用户提供实际正文内容。'

    # ============================================================
    # Rule 45: "引用的文献在正文中用方括号" — citation format
    # ============================================================
    if '引用的文献在正文中用方括号' in text:
        return 'requires_source_content', '引用标注格式要求，属于论文内容格式，需用户提供引用内容后应用。'

    # ============================================================
    # Rule 46: "参考文献书写格式要求请见"参考文献"部分" — reference format
    # ============================================================
    if '参考文献书写格式要求' in text:
        return 'requires_source_content', '参考文献格式要求，属于论文内容格式，需用户提供参考文献后应用。'

    # ============================================================
    # Rule 47: "必须符合国家标准规定" — national standard compliance
    # ============================================================
    if '必须符合国家标准规定' in text:
        return 'unverifiable', '国家标准合规要求，属于主观/外部标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 48: Unit names like "磁通量密度", "贝可［勒尔］", "皮［可］" — these are table entries
    # ============================================================
    unit_terms = ['磁通量密度', '磁感应强度', '贝可', '皮［可］', '贝可［勒尔］']
    if any(t in text for t in unit_terms) and len(text) < 30:
        return 'informational', '量和单位表中的条目内容，属于示例数据，非格式要求。'

    # ============================================================
    # Rule 49: "应使用全国自然科学名词审定委员会审定的自然科学名词术语" — terminology
    # ============================================================
    if '应使用全国自然科学名词审定委员会' in text:
        return 'unverifiable', '名词术语规范要求，属于主观/外部标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 50: "应按有关的标准或规定使用工程技术名词术语" — engineering terminology
    # ============================================================
    if '应按有关的标准或规定使用工程技术名词术语' in text:
        return 'unverifiable', '工程技术名词术语规范要求，属于主观/外部标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 51: "应使用公认共知的尚无标准或规定的名词术语" — recognized terminology
    # ============================================================
    if '应使用公认共知的尚无标准或规定的名词术语' in text:
        return 'unverifiable', '公认名词术语要求，属于主观标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 52: "外国人名可使用原文，不必译出" — foreign name format
    # ============================================================
    if '外国人名可使用原文，不必译出' in text:
        return 'unverifiable', '外国人名格式要求，属于主观标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 53: "数字的使用必须符合新的国家标准" — number format standard
    # ============================================================
    if '数字的使用必须符合' in text and '国家标准' in text:
        return 'unverifiable', '数字使用国家标准要求，属于主观/外部标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 54: "在特定场合中视为常数的参数" — parameter notation
    # ============================================================
    if '在特定场合中视为常数的参数' in text:
        return 'unverifiable', '参数符号规范要求，属于主观标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 55: "量符号中为区别其它量而加的" — quantity symbol notation
    # ============================================================
    if '量符号中为区别其它量而加的' in text:
        return 'unverifiable', '量符号下角标规范要求，属于主观标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 56: "文中涉及的量和单位一律采用新的国家标准" — units standard
    # ============================================================
    if '文中涉及的量和单位一律采用' in text and '国家标准' in text:
        return 'unverifiable', '量和单位国家标准要求，属于主观/外部标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 57: "标点符号的使用必须符合新的国家标准" — punctuation standard
    # ============================================================
    if '标点符号的使用必须符合' in text:
        return 'unverifiable', '标点符号国家标准要求，属于主观/外部标准，无法通过自动化工具客观验证。'

    # ============================================================
    # Rule 58: "定理：设函数在区间" — math theorem sample
    # ============================================================
    if '定理：' in text and '函数' in text:
        return 'requires_source_content', '数学定理示例内容，属于论文内容，需用户提供实际定理内容。'

    # ============================================================
    # Rule 59: "函数的极大值与极小值" — math definition sample
    # ============================================================
    if '函数的极大值与极小值' in text:
        return 'requires_source_content', '数学定义示例内容，属于论文内容，需用户提供实际定义内容。'

    # ============================================================
    # Rule 60: "标量要采用正体，而向量要采用黑体" — math notation
    # ============================================================
    if '标量要采用正体' in text and '向量要采用黑体' in text:
        return 'requires_source_content', '数学符号格式要求（标量正体、向量黑体），属于论文内容格式，需用户提供公式内容后应用。'

    # ============================================================
    # Rule 61: "由于直接在Word中编写大量代码" — code insertion note
    # ============================================================
    if '由于直接在Word中编写大量代码' in text:
        return 'informational', '代码插入方式说明，属于模板使用建议，非格式要求。'

    # ============================================================
    # Rule 62: "注意控制目录首页为奇数页" — odd page note for TOC
    # ============================================================
    if '注意控制目录首页为奇数页' in text:
        return 'informational', '模板提示控制目录首页为奇数页，属于排版操作说明。'

    # ============================================================
    # Rule 63: "总而言之保证下一节首页为奇数页" — odd page note
    # ============================================================
    if '总而言之保证下一节首页为奇数页' in text:
        return 'informational', '模板提示确保下一节首页为奇数页，属于排版操作说明。'

    # ============================================================
    # Rule 64: "页码应由绪论首页开始" — page numbering
    # ============================================================
    if '页码应由绪论首页开始' in text:
        return 'requires_source_content', '页码编排要求（绪论开始为第1页），属于文档结构要求，需在用户提供完整内容后设置。'

    # ============================================================
    # Rule 65: "页码必须标注在每页页脚底部居中位置" — page number position
    # ============================================================
    if '页码必须标注在每页页脚底部居中位置' in text:
        return 'requires_source_content', '页码位置格式要求，属于文档结构要求，需在用户提供完整内容后设置。'

    # ============================================================
    # Rule 66: "页眉位置" / "学位论文题目" — header content
    # ============================================================
    if '页眉位置' in text and '学位论文题目' in text:
        return 'requires_metadata', '页眉内容要求（替换为论文题目），属于用户输入元数据，需用户提供论文题目后设置。'

    # ============================================================
    # Rule 67: "(1) 每章的章标题选用"标题1"样式" — already covered above, but let's catch variants
    # ============================================================
    if '标题1' in text and '样式' in text:
        return 'requires_source_content', '标题样式格式要求，属于论文内容格式，需用户提供章节内容后应用。'

    # ============================================================
    # Rule 68: "(3) 对 齐：采用两边对齐" — alignment
    # ============================================================
    if '对齐' in text and '采用两边对齐' in text:
        return 'requires_source_content', '对齐方式要求，属于论文内容格式，需用户提供内容后应用。'

    # ============================================================
    # Rule 69: "(4) 软件要求" — software requirement
    # ============================================================
    if '软件要求' in text and 'Microsoft word' in text:
        return 'external_compliance', '软件要求（Microsoft Word 2003以上），属于外部环境要求，非DOCX格式要求。'

    # ============================================================
    # Rule 70: "(5) 如图 2.所示，为便于排版，请显示"编辑标记"" — show edit marks
    # ============================================================
    if '显示' in text and '编辑标记' in text:
        return 'informational', '模板建议显示编辑标记以便排版，属于操作建议，非格式要求。'

    # ============================================================
    # Rule 71: "(6) 样式设置方法" — style setting method
    # ============================================================
    if '样式设置方法' in text:
        return 'informational', '样式设置方法说明，属于模板使用指导，非格式要求。'

    # ============================================================
    # Rule 72: "正文格式进行修改，可选中" — style modification instruction
    # ============================================================
    if '正文格式进行修改' in text:
        return 'informational', '正文格式修改方法说明，属于模板使用指导，非格式要求。'

    # ============================================================
    # Rule 73: "正文格式进行设置，设置方法如图" — style setting instruction
    # ============================================================
    if '正文格式进行设置' in text and '设置方法如图' in text:
        return 'informational', '正文格式设置方法说明，属于模板使用指导，非格式要求。'

    # ============================================================
    # Rule 74: "正文"，可选中这段文字后点击" — applying style instruction
    # ============================================================
    if '正文' in text and '可选中这段文字后点击' in text:
        return 'informational', '正文样式应用方法说明，属于模板使用指导，非格式要求。'

    # ============================================================
    # Rule 75: "正文"模块即可，设置方法如图" — style application instruction
    # ============================================================
    if '正文' in text and '模块即可' in text:
        return 'informational', '正文样式应用方法说明，属于模板使用指导，非格式要求。'

    # ============================================================
    # Rule 76: "二级标题、图、表等的格式修改与设置方法同理" — style setting note
    # ============================================================
    if '二级标题、图、表等的格式修改与设置方法同理' in text:
        return 'informational', '样式设置方法说明（其他元素同理），属于模板使用指导，非格式要求。'

    # ============================================================
    # Rule 77: "正文"样式：每段落首行缩进2字" — body text style
    # ============================================================
    if '正文' in text and '样式' in text and '首行缩进' in text:
        return 'requires_source_content', '正文样式格式要求（首行缩进、字体、字号、行距），属于论文内容格式，需用户提供正文内容后应用。'

    # ============================================================
    # Rule 78: "可直接双面打印" — printing instruction
    # ============================================================
    if '可直接双面打印' in text:
        return 'informational', '双面打印说明，属于模板使用提示，非格式要求。'

    # ============================================================
    # Fallback: If we can't determine, keep as unresolved
    # ============================================================
    return 'unresolved', f'无法从现有证据确定为可执行要求、输入前置条件或纯说明，保留人工裁决。文本: {text[:100]}'


import re

# Process all target items
for r in targets:
    old_class = r['classification']
    new_class, reason = classify_clause(r['clause_id'], r)

    # Validate
    assert new_class in VALID_CLASSES, f"Illegal class '{new_class}' for {r['clause_id']}"
    assert reason and len(reason.strip()) > 0, f"Blank reason for {r['clause_id']}"

    # Check we don't set executable without requirement index
    if new_class == 'executable':
        assert len(r.get('requirement_indexes', [])) > 0, f"executable without requirement index for {r['clause_id']}"

    # Don't modify requirements field
    # Don't set executable if no requirement index (already checked above)

    if new_class != old_class:
        changes.append({
            'clause_id': r['clause_id'],
            'before': old_class,
            'after': new_class,
            'reason': reason
        })
        r['classification'] = new_class
        r['reason'] = reason

# Final validation
after_counts = Counter(r['classification'] for r in reviews)
print(f"After counts: {dict(after_counts)}")
print(f"Changes made: {len(changes)}")

# Verify all
blank_reasons = [r for r in reviews if not r.get('reason', '').strip()]
illegal_classes = [r for r in reviews if r['classification'] not in VALID_CLASSES]
invalid_req_idx = [r for r in reviews if r['classification'] == 'executable' and len(r.get('requirement_indexes', [])) == 0]

print(f"Blank reasons: {len(blank_reasons)}")
print(f"Illegal classes: {len(illegal_classes)}")
print(f"Invalid requirement indexes: {len(invalid_req_idx)}")

assert len(blank_reasons) == 0, f"Found {len(blank_reasons)} blank reasons"
assert len(illegal_classes) == 0, f"Found {len(illegal_classes)} illegal classes"
assert len(invalid_req_idx) == 0, f"Found {len(invalid_req_idx)} invalid req indexes"

# Verify all 1186 reviews have unique IDs matching clause IDs
final_ids = set(r['clause_id'] for r in reviews)
assert len(reviews) == 1186
assert len(final_ids) == 1186
assert final_ids == clause_ids

# Write response
with open(RESPONSE_PATH, 'w') as f:
    json.dump(response, f, ensure_ascii=False, indent=2)

# Build audit
audit = {
    'contract_version': '2.1',
    'changed_count': len(changes),
    'before_counts': dict(before_counts),
    'after_counts': dict(after_counts),
    'changes': changes
}

with open(AUDIT_PATH, 'w') as f:
    json.dump(audit, f, ensure_ascii=False, indent=2)

print(f"\nWritten: {RESPONSE_PATH}")
print(f"Written: {AUDIT_PATH}")
print(f"\nSummary: {len(reviews)} reviews, {len(final_ids)} unique IDs, {len(changes)} changes, 0 blank reasons, 0 illegal classes, 0 invalid req indexes")
