---
name: to-issues
description: 将计划/PRD 拆分为可独立领取的 issue（tracer bullet 垂直切片），写入本地 issues/ 目录。纯 batch 模式，不发布到任何 issue tracker，不与用户交互。
---

# To Issues (Local Files)

将计划/PRD/架构拆分为可独立领取的 issue，使用 **tracer bullet 垂直切片**方式，**写入当前工作目录的 `issues/` 目录**。

本 skill 是本地 batch 流水线版本：
- ❌ 不发布到任何 issue tracker（GitHub/Jira）
- ❌ 不与用户交互确认拆分粒度
- ✅ 每个 issue 写一个 markdown 文件到 `issues/NN-<slug>.md`
- ✅ 全部决策由 CC 自对话完成

## Process

### 1. Gather context

从上游文档（PRD、计划、架构）提取要拆分的内容。如果用户传入 issue 引用（编号/URL/路径），读取其完整内容。

### 2. Explore codebase (optional)

如果还没探索过代码库，先探索以理解现状。Issue 标题和描述应使用项目领域词汇表（`CONTEXT.md`），尊重相关 ADR。

寻找 prefactor 机会——"先让改动变容易，再做容易的改动"。

### 3. Draft vertical slices (自对话决策，不问用户)

把计划拆成 **tracer bullet** issue。每个 issue 是一条窄但**完整**的垂直切片，不是单层的水平切片。

<vertical-slice-rules>

- 每条切片要贯穿所有集成层（schema、API、UI、tests），端到端窄但完整
- 完成后可独立 demo 或验证
- 任何 prefactor 优先做（作为单独 issue）

</vertical-slice-rules>

自对话决策标准（不问用户，自行判断）：
- **粒度**：每条切片 1 个 CC 1~2 小时可完成。太粗就拆，太细就合
- **依赖**：被依赖的切片排在前面，用 `Blocked by` 标注
- **覆盖**：所有用户故事都要有切片覆盖，不能遗漏
- **prefactor**：如果需要 prefactor，作为第 0 号 issue 优先做

### 4. Write issues

按依赖顺序写入 `issues/` 目录（当前工作目录下），每个 issue 一个文件：`issues/NN-<short-slug>.md`，NN 从 01 开始。

依赖顺序：blocker 在前，被 block 的在后，这样后面的 issue 可以引用前面 issue 的真实编号。

## Issue File Template

```markdown
# Issue NN: <title>

## Parent

父 issue 引用（如果源是已有 issue，否则省略此节）。

## What to build

本垂直切片的简洁描述。描述端到端行为，不要逐层描述实现。

不要写具体文件路径或代码片段——它们很快过时。例外：如果原型里有一段 snippet 精确编码了某个决策（状态机、reducer、schema、type shape），可以内联并标注来自原型。只保留决策密集部分。

## Acceptance criteria

- [ ] 验收条件 1
- [ ] 验收条件 2
- [ ] 验收条件 3

## Blocked by

- Issue NN（如果有阻塞）
- 或写 "None - can start immediately"

## Status

ready-for-agent
```

## Quality Requirements

- 每条切片必须端到端可验证，不能是单层水平切片（如"建 schema"、"写 API"、"做 UI"分3条是错的）
- 验收条件必须可执行验证，不能写"功能正常"
- `Blocked by` 必须引用真实存在的 issue 编号
- prefactor issue 排在最前
- 不要写具体文件路径或代码片段（容易过时）
- 直接写入 `issues/NN-<slug>.md`，不需要额外说明
