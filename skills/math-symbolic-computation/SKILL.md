---
name: math-symbolic-computation
description: 用 SymPy 与 NumPy 做符号与数值计算的实用指南。用于化简、求导、求极限、级数展开、解方程、数值验证与反例搜索。当需要实际运行符号/数值计算来验证猜想或生成算例时使用。
---

# 符号与数值计算指南（SymPy / NumPy）

原则：所有结果必须由实际运行产出，报告命令与输出关键行，不手填结果。符号结果要独立验证，数值结果要报告误差。

## 符号计算（SymPy）

```python
import sympy as sp

x, n = sp.symbols('x n', real=True)   # 声明实数可帮助化简
y = sp.Function('y')

sp.simplify(expr)                     # 化简
sp.expand(expr)                       # 展开
sp.factor(expr)                       # 因式分解
sp.diff(expr, x)                      # 求导
sp.diff(expr, x, 2)                   # 高阶导
sp.integrate(expr, x)                 # 不定积分
sp.integrate(expr, (x, 0, 1))         # 定积分
sp.limit(expr, x, sp.oo)              # 极限
sp.series(expr, x, 0, 6)              # 级数展开（含 O 项）
sp.solve(sp.Eq(expr, 0), x)           # 解方程
sp.solveset(expr, x, sp.S.Reals)      # 集合式求解
sp.summation(expr, (n, 0, 10))        # 求和
sp.simplify(sp.diff(sp.integrate(expr, x), x))  # 微积分基本定理自检
```

## 常用自检手法

- **微分反检**：对积分结果求导，应回到被积函数。
- **特值代入**：把化简前后的表达式代入具体数值，应相等。
- **极限核对**：数值逼近极限，与符号极限对照。
- **级数验证**：级数展开与数值求和对照低阶项。

## 数值计算（NumPy / mpmath）

```python
import numpy as np
from mpmath import mp
mp.dps = 30                          # 高精度，避免浮点误判

np.sum(arr)                          # 求和
np.prod(arr)                         # 求积
```

- 数值结果是**近似**，必须报告误差或精度来源，不得当严格等式陈述。
- 高精度计算用 `mpmath`（`mp.dps` 设 30+），避免浮点舍入导致错误结论。
- 数值验证只能提供**证据**或**反例**，不能替代严格证明。

## 反例与穷举搜索

```python
# 有限情形穷举：报告搜索范围与命中结果
candidates = range(-100, 101)
hits = [k for k in candidates if cond(k)]
```

- 穷举必须报告实际范围与命中结果，不夸大覆盖。
- 找到反例即证伪，报告具体反例与其不满足的条件。

## 可复现

- 固定随机种子：`np.random.seed(42)`。
- 声明依赖版本（sympy / numpy / mpmath 版本）。
- 结果要能被第三者在只读交付物下按命令重跑。

## 输出约定

每个计算块写清：要回答的问题 → 代码要点 → 运行输出关键行 → 结论。结论与输出一致，不手填。
