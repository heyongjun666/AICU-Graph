---
name: codegraph
description: 代码知识图谱(code knowledge graph)。当你需要快速理解代码库结构、跨文件调用链、某个符号的定义/调用者/被调用者、修改某段代码的影响面(blast radius)、或在不逐文件 Read 的情况下定位相关代码时使用。典型触发词：这个功能是怎么实现的 / 谁调用了这个方法 / 改动 X 会影响哪些地方 / 帮我快速定位 / 代码导航。
---

# CodeGraph — 代码知识图谱查询

CodeGraph 为当前项目预先建立了本地代码索引（符号、跨文件引用、调用边、依赖），
你不需要 grep/glob/Read 逐个文件摸索结构，一次 `codegraph` 调用即可拿到精确的代码上下文。

## 什么时候用

- 需要理解"某个功能是怎么实现的 / 数据是怎么流转的" → 用 **explore**
- 需要看某个符号的定义源码及其调用者 → 用 **node**
- 需要知道"谁调用了这个函数 / 这个函数调用了谁" → 用 **callers / callees**
- 需要评估"改这段代码会影响哪些地方 / 会不会破坏其他模块" → 用 **impact**
- 需要列出项目文件结构 / 查询符号 → 用 **files / query**

## 核心命令（都在当前目录或项目根下执行）

**首选：一次搞定的探索**
```bash
codegraph explore "<自然语言问题>"
```
一次返回：相关符号的逐字源码（按文件分组）+ 符号间调用路径 + 影响面摘要。
能追到动态派发(grep 追不到的 callback/接口实现跳转)。大多数场景这一个命令就够了。

**看单个符号/文件**
```bash
codegraph node <符号名|文件路径>
```
符号 → 源码 + 调用者；文件 → 带行号的完整内容（等价于 Read）。

**调用关系**
```bash
codegraph callers <函数/方法名>     # 谁调用它
codegraph callees <函数/方法名>     # 它调用了谁
codegraph impact <符号名>           # 改动它会影响哪些代码(blast radius)
```

**搜索与浏览**
```bash
codegraph query <关键字>            # 按名字搜符号(--kind/--limit/--json)
codegraph files [路径]              # 文件结构树
codegraph status                    # 索引统计(节点/边/语言)
```

**辅助参数**：查询命令末尾可直接追加项目根路径（不带参数时自动从当前目录向上找最近的 `.codegraph/`）。

## 项目路径解析

- 本项目已索引，索引根在项目根目录的 `.codegraph/`。
- 你在自身 workspace 子目录执行时，codegraph 会自动向上找到项目根的索引，无需传路径。
- 若在索引根之外执行，显式追加项目根路径：
  `<项目根绝对路径>`（如 `/path/to/your/project`）

## 索引新鲜度

- 自动增量同步默认开启：文件改动后索引亚秒级更新，通常无需手动操作。
- 若改动非常新且结果疑似过期，执行 `codegraph sync` 强制增量刷新。

## 尚未索引时

在项目根执行一次初始化（会建立索引并开启自动同步）：
```bash
codegraph init /path/to/your/project
```

## 故障排查

- `codegraph: command not found` → 未安装：`npm i -g @colbymchenry/codegraph`，
  并注意本机旧 glibc 需要用 gcc-12 loader 补丁（见项目内说明）。
- 报 `GLIBC_2.27 not found` 之类的动态库错误 → bundled node 需要较新 glibc，
  用 `patchelf` 把 `codegraph-linux-x64/node` 的 interpreter/rpath 指到
  `/home/opt/compiler/gcc-12/lib` 即可。
- `Not initialized` → 先执行上面的 `codegraph init`。
