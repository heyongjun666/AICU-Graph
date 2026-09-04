---
name: to-prd
description: 将当前对话/上游文档综合为 PRD，写入本地 prd.md 文件。纯 batch 模式，不发布到任何 issue tracker，不与用户交互。
---

# To PRD (Local File)

将已有上下文（需求文档、原型、对话）综合为一份 PRD，**写入当前工作目录的 `prd.md`**。

本 skill 是本地 batch 流水线版本：
- ❌ 不发布到任何 issue tracker（GitHub/Jira）
- ❌ 不与用户交互确认 seam
- ✅ 只生成 `prd.md` 文件
- ✅ 全部决策由 CC 自对话完成

## Process

### 1. Gather context

读取上游文档（需求文档、原型设计、架构设计等）。如果已有 `CONTEXT.md` 和 `docs/adr/`，使用其中的领域词汇，并尊重相关 ADR。

### 2. Decide test seams

自行识别测试 seam（不问用户）。原则：
- 优先复用已有 seam，不要新建
- 选最高层 seam（端到端 > 集成 > 单元）
- seam 数量越少越好，理想是 1 个
- 如果需要新 seam，在最高可行点提出

将 seam 决策写入 PRD 的 "Testing Decisions" 小节，并简短说明为什么选这个 seam。

### 3. Write PRD

用下面的模板，把 PRD 写入 `prd.md`（当前工作目录下）。

## PRD Template

```markdown
# PRD: <feature name>

## Problem Statement

从用户视角描述要解决的问题。

## Solution

从用户视角描述解决方案。

## User Stories

编号列表，覆盖所有方面：

1. As a <actor>, I want <feature>, so that <benefit>
2. ...

## Implementation Decisions

- 要构建/修改的模块
- 模块接口
- 技术澄清
- 架构决策
- Schema 变更
- API 契约
- 具体交互

不要写具体文件路径或代码片段（容易过时）。

例外：如果原型里有一段 snippet 精确编码了某个决策（状态机、reducer、schema、type shape），可以内联到相关决策里，标注来自原型。只保留决策密集部分，不是完整 demo。

## Testing Decisions

- 什么算好测试（只测外部行为，不测实现细节）
- 哪些模块要测
- 测试 seam 选择（最高层优先）+ 理由
- 现有类似测试参考

## Out of Scope

明确不在本 PRD 范围内的事项。

## Further Notes

其他需要说明的。
```

## Quality Requirements

- User Stories 必须**详尽**，覆盖功能所有方面，不要只列 3-5 条
- Implementation Decisions 必须具体（模块名、接口签名），不能写"采用合适的架构"
- Testing Decisions 必须给出 seam 选择 + 理由，不能只写"写单元测试"
- 不要包含具体文件路径或代码片段（容易过时）
- 直接写入 `prd.md`，不需要额外说明
