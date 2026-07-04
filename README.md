# thesis-latex2docx

V2 通用论文 LaTeX → DOCX 管线（多校可配 schema）

## 设计理念

- **单一 schema，多校 overlay**：`config-schema-v2.yaml` 定义完整的通用论文配置结构，每个学校只需提供一个轻量 overlay（如 `config-tjufe-overlay.example.yaml`）覆盖差异字段。
- **LLM 辅助提取**：由 LLM 从学校官方模板规范中提取字段值，经人工审核后形成 overlay。
- **mapper 桥接**：`mapper-v2-to-tjufe.py` 将 V2 schema 映射为下游 Pandoc 管线可用的 metadata。

## 目录结构

```
schema/                          # 配置层
  config-schema-v2.yaml          # V2 通用 schema（定义 + 文档 + 默认值）
  config-tjufe-overlay.example.yaml  # 学校 overlay 示例（脱敏版）
scripts/                         # 工具脚本
  mapper-v2-to-tjufe.py          # V2 schema → TJufe Pandoc metadata
csl/                             # 引文样式（GB/T 7714-2015）
filters/                         # Lua 过滤器（开发中）
prompts/                         # LLM prompt 模板（开发中）
```

## 快速开始

1. 复制 overlay 示例并填入学校参数：
   ```bash
   cp schema/config-tjufe-overlay.example.yaml schema/config-my-school-overlay.yaml
   ```
2. 编辑 overlay 文件
3. 运行 mapper 生成下游 metadata：
   ```bash
   python3 scripts/mapper-v2-to-tjufe.py schema/config-schema-v2.yaml schema/config-my-school-overlay.yaml > metadata.yaml
   ```

## 状态

- ✅ V2 通用 schema（config-schema-v2.yaml）
- ✅ 天财 overlay 及 mapper 桥接
- ⏳ Lua 过滤器
- ⏳ DOCX 后处理脚本
- ⏳ 集成测试
