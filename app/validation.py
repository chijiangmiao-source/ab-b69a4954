"""审计载荷的严格校验。

所有拒绝都抛出 :class:`ValidationError`（携带面向用户的中文消息与
机器可读错误码）；校验失败时调用方不得写入或覆盖任何冻结记录。
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

from .simplex import Constraint

MIN_VARS = 1
MAX_VARS = 8
MAX_CONSTRAINTS = 48
# 任意精度运算可以处理更大的整数，这里限制整数数字位数仅用于防止
# 恶意超大整数造成资源耗尽；不是数值上的近似。
MAX_INT_DIGITS = 1000
MAX_NAME_LEN = 32
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")


class ValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_plain_int(v: Any) -> bool:
    # bool 是 int 的子类，必须显式拒绝；拒绝 float/str。
    return isinstance(v, int) and not isinstance(v, bool)


def _check_int(v: Any, where: str) -> int:
    if not _is_plain_int(v):
        raise ValidationError("not_integer", f"{where} 必须是整数")
    if len(str(abs(v))) > MAX_INT_DIGITS:
        raise ValidationError(
            "integer_too_large", f"{where} 的绝对值超过 {MAX_INT_DIGITS} 位数字上限"
        )
    return v


def validate_audit_id(v: Any) -> str:
    if not isinstance(v, str) or not ID_RE.match(v):
        raise ValidationError(
            "invalid_audit_id",
            "审计编号须为 1-64 位字母、数字、点、下划线或短横线，且以字母数字开头",
        )
    return v


def validate_payload(payload: Any) -> Tuple[str, List[str], List[Constraint]]:
    """返回 (audit_id, 变量名列表, Constraint 列表)。

    成功返回的所有整数均为 Python 任意精度 int。
    """
    if not isinstance(payload, dict):
        raise ValidationError("invalid_json", "请求体必须是 JSON 对象")

    audit_id = validate_audit_id(payload.get("audit_id"))

    raw_vars = payload.get("variables")
    if not isinstance(raw_vars, list) or not (MIN_VARS <= len(raw_vars) <= MAX_VARS):
        raise ValidationError(
            "bad_variables", f"variables 必须是 {MIN_VARS}-{MAX_VARS} 个变量的数组"
        )
    names: List[str] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_vars):
        # 变量可以是字符串名字，也可以是 {"name": ...}；默认 x1..。
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            name = item["name"]
        else:
            raise ValidationError(
                "bad_variable_name", f"第 {i + 1} 个变量名必须是字符串"
            )
        if not NAME_RE.match(name) or len(name) > MAX_NAME_LEN:
            raise ValidationError(
                "bad_variable_name",
                f"变量名 {name!r} 非法（至多 {MAX_NAME_LEN} 个非控制字符）",
            )
        if name in seen:
            raise ValidationError("duplicate_variable", f"变量名 {name!r} 重复")
        seen.add(name)
        names.append(name)

    raw_constraints = payload.get("constraints")
    if not isinstance(raw_constraints, list):
        raise ValidationError("bad_constraints", "constraints 必须是数组")
    if len(raw_constraints) > MAX_CONSTRAINTS:
        raise ValidationError(
            "too_many_constraints", f"约束数量不得超过 {MAX_CONSTRAINTS} 条"
        )

    constraints: List[Constraint] = []
    for i, c in enumerate(raw_constraints):
        where = f"第 {i + 1} 条约束"
        if not isinstance(c, dict):
            raise ValidationError("bad_constraint", f"{where} 必须是对象")
        coeffs = c.get("coeffs")
        if not isinstance(coeffs, list) or len(coeffs) != len(names):
            raise ValidationError(
                "bad_coeffs", f"{where} 的 coeffs 长度必须等于变量个数 {len(names)}"
            )
        row = tuple(_check_int(v, f"{where} 的第 {j + 1} 个系数") for j, v in enumerate(coeffs))
        b = _check_int(c.get("rhs"), f"{where} 的 rhs")
        stable = c.get("stable", False)
        if not isinstance(stable, bool):
            raise ValidationError("bad_stable", f"{where} 的 stable 标识必须是布尔值")
        constraints.append(Constraint(coeffs=row, rhs=b, stable=stable))

    return audit_id, names, constraints
