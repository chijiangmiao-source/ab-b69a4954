"""任意精度有理数 Phase-I 单纯形。

输入：m 条整数不等式 ``A x <= b``，x 为自由变量（电流可正可负）。
输出：

* 可行：一组有理数解 ``x``（由固定枢轴/Bland 规则确定，结果可复现），
  以及每条约束的精确余量 ``b - A x``（非负）。
* 不可行：非负有理乘子 ``pi``，满足
  ``sum_i pi_i * A_i = 0`` 且 ``sum_i pi_i * b_i < 0``
  （Farkas 不可行性证书），并逐项给出加权后的合并式。

不使用任何浮点运算；所有量均为 :class:`fractions.Fraction` 或 :class:`int`。
不调用任何外部 LP 求解器、采样或启发式方法。

构造（合法初始基的 Phase-I）
----------------------------

1. 第 i 行加入松弛变量 t_i >= 0： ``a_i x + t_i = b_i``。
2. 若 b_i < 0，整行乘 -1，使右端非负；记 s_i = sign(b_i)（b_i=0 取 +1）。
   标准化等式行： ``s_i a_i x + s_i t_i + y_i = s_i b_i = |b_i|``，
   即人工变量补足松弛后的缺口（y_i >= 0）。
3. 每行加人工变量 y_i >= 0（该行系数 1、其余行 0），初始基即 y_i，
   因 RHS 非负而可行。
4. 最小化 w = sum_i y_i：目标行写成方程  w - sum y_i = 0，再逐行加上
   数据行把人工列清零。随后用单纯形最小化 w。

自由变量 x_j 编码为 x_j = p_j - n_j（p_j, n_j >= 0）。列序固定为
``[p_0,n_0, p_1,n_1, ..., t_0..t_{m-1}, y_0..y_{m-1} | RHS | 凭证列]``。

枢轴全程使用 Bland 规则：

* 入基：非基列按下标升序，取第一个既约费用为正（目标行系数 > 0）者；
* 出基：比值 RHS/a_ir（a_ir>0）最小者，平手取基变量下标最小的行。

Bland 规则保证退化（零步长枢轴）下也有限步终止，不循环。

凭证
----

表的 RHS 之后拼接 m 个"凭证列"：数据行 i 的凭证初值为第 i 个单位向量，
目标行凭证初值为全 1（因为初始目标行在清人工列时等于所有数据行之和）。
凭证列随每次枢轴与表做完全相同的行变换，故终表任意一行都精确记录它是
哪些初始标准化等式行的线性组合。不可行（w* > 0）时，目标行凭证 λ >= 0
给出 Farkas 组合，映射回原约束的乘子为  pi_i = -lambda_i * s_i。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Sequence


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Feasible:
    status: str  # 固定为 "feasible"
    x: List[Fraction]
    slacks: List[Fraction]


@dataclass(frozen=True)
class Infeasible:
    status: str  # 固定为 "infeasible"
    multipliers: List[Fraction]
    lhs: List[Fraction]
    rhs: Fraction
    terms: List[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Constraint:
    coeffs: tuple[int, ...]
    rhs: int
    stable: bool


# ---------------------------------------------------------------------------
# 枢轴
# ---------------------------------------------------------------------------


def _pivot(table: List[List[Fraction]], r: int, c: int) -> None:
    """以 table[r][c] 为主元做高斯-若当消元（含目标行与凭证列）。"""
    pivot = table[r][c]
    table[r] = [v / pivot for v in table[r]]
    pivot_row = table[r]
    for i, row in enumerate(table):
        if i == r:
            continue
        factor = row[c]
        if factor:
            table[i] = [v - factor * rv for v, rv in zip(row, pivot_row)]


# ---------------------------------------------------------------------------
# Phase-I 单纯形
# ---------------------------------------------------------------------------


def phase_one_simplex(
    A: Sequence[Sequence[int]], b: Sequence[int]
) -> Feasible | Infeasible:
    """对 ``A x <= b`` 执行精确有理数 Phase-I 单纯形。"""
    m = len(b)
    k = len(A[0]) if m else 0

    n_decision = 2 * k
    col_t = n_decision              # 松弛变量起始列
    col_y = n_decision + m          # 人工变量起始列
    col_rhs = n_decision + 2 * m    # RHS 列
    col_cred = col_rhs + 1          # 凭证列起始（共 m 列）
    total_cols = col_cred + m

    def dcol_p(j: int) -> int:
        return 2 * j

    def dcol_n(j: int) -> int:
        return 2 * j + 1

    orientation: List[int] = []
    # table[0] 为目标行；table[1..m] 为数据行。
    table: List[List[Fraction]] = [
        [Fraction(0) for _ in range(total_cols)] for _ in range(m + 1)
    ]

    for i in range(m):
        row = table[i + 1]
        s = 1 if b[i] >= 0 else -1
        orientation.append(s)
        for j, a in enumerate(A[i]):
            row[dcol_p(j)] = Fraction(s * a)
            row[dcol_n(j)] = Fraction(-s * a)
        row[col_t + i] = Fraction(s)
        row[col_y + i] = Fraction(1)
        row[col_rhs] = Fraction(s * b[i])  # = |b_i| >= 0
        row[col_cred + i] = Fraction(1)    # 单位凭证

    # 目标行：w - sum_i y_i = 0，再逐行加数据行清掉人工列系数。
    z = table[0]
    for i in range(m):
        z[col_y + i] = Fraction(-1)
    for i in range(m):
        src = table[i + 1]
        for c in range(total_cols):
            z[c] += src[c]
    # 此时目标行：w + sum_i s_i a_i (p-n) + sum_i s_i t_i = sum_i |b_i|，
    # 人工列系数全为 0；凭证列全为 1（= 所有初始数据行之和）。

    basis: List[int] = [col_y + i for i in range(m)]

    # ---- Bland 单纯形主循环（最小化 w）----
    while True:
        z = table[0]
        enter = -1
        # 只在决策列与松弛列中选入基列；人工变量列在目标行中系数为 0，
        # 且重新引入人工变量没有意义。
        nonbasic_scan = set(range(col_y)) - set(basis)
        for c in range(col_y):
            if c not in nonbasic_scan:
                continue
            if z[c] > 0:
                enter = c
                break
        if enter < 0:
            break  # 最优

        leave = -1
        best_ratio: Fraction | None = None
        for i in range(m):
            a = table[i + 1][enter]
            if a <= 0:
                continue
            ratio = table[i + 1][col_rhs] / a  # RHS 恒非负 -> ratio >= 0
            if best_ratio is None or ratio < best_ratio:
                best_ratio = ratio
                leave = i
            elif ratio == best_ratio and basis[i] < basis[leave]:
                # Bland 平手：基变量下标最小者出基。
                leave = i
        if leave < 0:
            # w 在可行域内有下界 0，不可能无界；到达此处说明构造有误。
            raise RuntimeError("phase-I objective unbounded (invalid tableau)")

        _pivot(table, leave + 1, enter)
        basis[leave] = enter

    w_star = table[0][col_rhs]

    # ---- 读取解 ----
    values: dict[int, Fraction] = {}
    for i, bc in enumerate(basis):
        values[bc] = table[i + 1][col_rhs]

    if w_star == 0:
        x = [
            values.get(dcol_p(j), Fraction(0))
            - values.get(dcol_n(j), Fraction(0))
            for j in range(k)
        ]
        slacks = [
            Fraction(b[i])
            - sum(
                (Fraction(a) * x[j] for j, a in enumerate(A[i])),
                Fraction(0),
            )
            for i in range(m)
        ]
        assert all(s >= 0 for s in slacks), "slack negative despite w*=0"
        return Feasible(status="feasible", x=x, slacks=slacks)

    # ---- 不可行：由目标行凭证构造 Farkas 证书 ----
    #
    # λ_q = 终表目标行对初始标准化等式行 q 的组合系数（注意 λ 本身
    # 未必非负，例如 x<=1 与 -x<=-2 的证书中 λ=(-1, 1)）。
    #
    # 松弛列 t_q 在初始行中仅行 q 含非零系数 s_q（=±1），且行变换中
    # 松弛列不会成为枢轴列以外的干扰，故终表目标行在 t_q 列的系数恰为
    # λ_q s_q。最优时：
    #   * t_q 非基 -> 该既约费用 <= 0，即 λ_q s_q <= 0；
    #   * t_q 在基 -> 目标行系数为 0，即 λ_q s_q = 0。
    # 取原约束乘子  pi_q = -λ_q s_q，两种情形均有 pi_q >= 0。
    #
    # 决策列 p_j/n_j 在每一行中系数始终互为相反数（初始相反，行变换
    # 保持相反），故其既约费用相反；最优时同 <= 0 只能都等于 0，
    # （某列在基时其目标行为 0，配对列同样为 0）。于是
    #   * sum_q pi_q a_q = -sum_q λ_q s_q a_q = 0（决策列为零）；
    #   * sum_q pi_q b_q = -sum_q λ_q s_q b_q = -w* < 0
    #     （RHS 列：Σ λ_q |b_q| = w*，而 s_q b_q = |b_q|）。
    lam = [table[0][col_cred + q] for q in range(m)]

    multipliers = [-lam[q] * orientation[q] for q in range(m)]

    lhs = [Fraction(0) for _ in range(k)]
    rhs_sum = Fraction(0)
    for q in range(m):
        for j, a in enumerate(A[q]):
            lhs[j] += multipliers[q] * a
        rhs_sum += multipliers[q] * b[q]

    assert all(v == 0 for v in lhs), "certificate lhs not zero"
    assert rhs_sum < 0, "certificate rhs not strictly negative"
    assert rhs_sum == -w_star, "certificate rhs != -w*"
    assert all(v >= 0 for v in multipliers), "certificate multiplier negative"

    terms: List[dict] = []
    for q in range(m):
        if multipliers[q] == 0:
            continue
        terms.append(
            {
                "index": q,
                "multiplier": _frac(multipliers[q]),
                "orientation": orientation[q],
                "weighted_coeffs": [_frac(multipliers[q] * a) for a in A[q]],
                "weighted_rhs": _frac(multipliers[q] * b[q]),
            }
        )

    return Infeasible(
        status="infeasible",
        multipliers=multipliers,
        lhs=lhs,
        rhs=rhs_sum,
        terms=terms,
    )


# ---------------------------------------------------------------------------
# 有理数 JSON 序列化
# ---------------------------------------------------------------------------


def _frac(v: Fraction) -> dict:
    return {"num": v.numerator, "den": v.denominator}


def feasible_payload(result: Feasible, constraints: List[Constraint]) -> dict:
    return {
        "status": "feasible",
        "solution": [_frac(v) for v in result.x],
        "constraints": [
            {
                "index": i,
                "slack": _frac(result.slacks[i]),
                "binding": result.slacks[i] == 0,
                "stable": constraints[i].stable,
            }
            for i in range(len(constraints))
        ],
    }


def infeasible_payload(result: Infeasible) -> dict:
    return {
        "status": "infeasible",
        "certificate": {
            "multipliers": [_frac(v) for v in result.multipliers],
            "combined_lhs": [_frac(v) for v in result.lhs],
            "combined_rhs": _frac(result.rhs),
            "claim": "0 = sum(multipliers_i * a_i) 且 sum(multipliers_i * b_i) < 0",
            "terms": result.terms,
        },
    }
