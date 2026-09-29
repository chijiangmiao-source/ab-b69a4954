"""测试：单纯形正确性、Farkas 证书验证、HTTP 与冻结存储语义。

运行：``python -m pytest -q``（无 pytest 时也可 ``python tests/test_all.py``）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from fractions import Fraction

from app.simplex import phase_one_simplex
from app.storage import ConflictError, FrozenStore, canonical_hash
from app.validation import ValidationError, validate_payload


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _f(d):
    return Fraction(d["num"], d["den"])


def _cert_ok(A, b, res):
    """独立验证不可行证书：pi>=0, Σpi a =0, Σpi b < 0。"""
    assert res.status == "infeasible"
    k = len(A[0])
    pi = res.multipliers
    assert len(pi) == len(A) == len(b)
    assert all(v >= 0 for v in pi)
    lhs = [sum(pi[i] * A[i][j] for i in range(len(A))) for j in range(k)]
    rhs = sum(pi[i] * b[i] for i in range(len(A)))
    assert all(v == 0 for v in lhs)
    assert rhs < 0
    assert rhs == res.rhs
    # 逐项加权式与总和一致
    for t in res.terms:
        q = t["index"]
        assert _f(t["weighted_rhs"]) == pi[q] * b[q]
        for j in range(k):
            assert _f(t["weighted_coeffs"][j]) == pi[q] * A[q][j]
    return rhs


def _feasible_ok(A, b, res):
    assert res.status == "feasible"
    x = res.x
    assert len(x) == len(A[0])
    for i, row in enumerate(A):
        lhs = sum(row[j] * x[j] for j in range(len(x)))
        assert lhs <= b[i]
        assert res.slacks[i] == b[i] - lhs
        assert res.slacks[i] >= 0
    return x


# ---------------------------------------------------------------------------
# 单纯形
# ---------------------------------------------------------------------------


def test_simple_feasible():
    # x1 + x2 <= 3; x1 <= 2; -x2 <= 0 (x2 >= 0)
    A = [[1, 1], [1, 0], [0, -1]]
    b = [3, 2, 0]
    res = phase_one_simplex(A, b)
    _feasible_ok(A, b, res)


def test_simple_infeasible_certificate():
    # x <= 1 且 x >= 2
    A = [[1], [-1]]
    b = [1, -2]
    res = phase_one_simplex(A, b)
    rhs = _cert_ok(A, b, res)
    # 期望乘子 (1, 1)：合并得 0 <= -1
    assert res.multipliers == [Fraction(1), Fraction(1)]
    assert rhs == -1


def test_zero_le_minus_one():
    """verify 容器使用的核心用例：0 <= -1 单条约束即矛盾。"""
    A = [[0]]
    b = [-1]
    res = phase_one_simplex(A, b)
    rhs = _cert_ok(A, b, res)
    assert res.multipliers[0] == Fraction(1)
    assert rhs == -1


def test_zero_le_minus_one_embedded():
    # 多条约束中嵌入 0 <= -1，且有其他正常约束
    A = [[1, 0], [0, 0], [0, 1]]
    b = [2, -1, 3]
    res = phase_one_simplex(A, b)
    _cert_ok(A, b, res)


def test_free_variable_negative_currents():
    # x 可取负值（电流方向）：-x <= 5（x>=-5）, x <= 4，可行
    A = [[-1], [1]]
    b = [5, 4]
    res = phase_one_simplex(A, b)
    _feasible_ok(A, b, res)


def test_infeasible_needs_fractional_multipliers():
    # 证书乘子不是整数的情形
    A = [[2], [-1]]
    b = [1, -1]  # 2x<=1 即 x<=1/2；x>=1，矛盾
    res = phase_one_simplex(A, b)
    _cert_ok(A, b, res)
    # 2*pi1 - pi2 = 0, pi1 - pi2 < 0；证书给出 (1/2, 1)（与 (1,2) 成正倍数）
    assert res.multipliers == [Fraction(1, 2), Fraction(1)]


def test_degenerate_does_not_hang():
    """大量退化（零 RHS）约束：Bland 必须有限终止。"""
    # x1 = x2 = x3 = 0 的多面体重叠表示，再加矛盾
    A = [
        [1, 0, 0], [-1, 0, 0],
        [0, 1, 0], [0, -1, 0],
        [0, 0, 1], [0, 0, -1],
        [1, 1, 1], [-1, -1, -1],
        [0, 0, 0],
    ]
    b = [0, 0, 0, 0, 0, 0, 0, 0, -1]
    res = phase_one_simplex(A, b)
    _cert_ok(A, b, res)


def test_degenerate_feasible():
    A = [[1], [-1]]
    b = [0, 0]  # x = 0
    res = phase_one_simplex(A, b)
    x = _feasible_ok(A, b, res)
    assert x[0] == 0


def test_no_constraints():
    res = phase_one_simplex([], [])
    assert res.status == "feasible"
    assert res.x == []


def test_rational_solution_exact():
    # 2x <= 1, -2x <= -1 -> x = 1/2
    A = [[2], [-2]]
    b = [1, -1]
    res = phase_one_simplex(A, b)
    x = _feasible_ok(A, b, res)
    assert x[0] == Fraction(1, 2)


def test_determinism_same_input_same_output():
    A = [[3, -2, 1], [1, 1, 1], [-2, 0, 1], [0, -1, -1]]
    b = [7, 4, -1, 0]
    r1 = phase_one_simplex(A, b)
    r2 = phase_one_simplex(A, b)
    if r1.status == "feasible":
        assert r1.x == r2.x
    else:
        assert r1.multipliers == r2.multipliers


def test_big_integers_exact():
    # 任意精度：大整数上 0 <= -10^60 仍精确矛盾
    big = 10**60
    A = [[big, -big], [-big, big]]
    b = [big, -big - 1]
    res = phase_one_simplex(A, b)
    _cert_ok(A, b, res)


def test_max_size_problem():
    # 8 变量 48 约束的可行问题
    k, m = 8, 48
    A = [[((i * 7 + j * 3) % 5) - 2 for j in range(k)] for i in range(m)]
    b = [1000 + i for i in range(m)]
    res = phase_one_simplex(A, b)
    _feasible_ok(A, b, res)


# ---------------------------------------------------------------------------
# 独立 oracle：Fourier-Motzkin 消元（有理数），用于随机交叉验证
# ---------------------------------------------------------------------------


def _fm_feasible(rows):
    """rows: [(coeffs_tuple, rhs)]，返回 True/False。"""
    rows = [(tuple(Fraction(x) for x in a), Fraction(bb)) for a, bb in rows]
    while rows:
        k = len(rows[0][0])
        if k == 0:
            return all(0 <= bb for _, bb in rows)
        # 选非零系数最少的变量消去
        best_var, best_count = 0, None
        for j in range(k):
            cnt = sum(1 for a, _ in rows if a[j] != 0)
            if cnt == 0:
                continue
            if best_count is None or cnt < best_count:
                best_var, best_count = j, cnt
        if best_count is None:
            return all(0 <= bb for _, bb in rows)
        j = best_var
        pos, neg, zero = [], [], []
        for a, bb in rows:
            if a[j] > 0:
                pos.append((a, bb))
            elif a[j] < 0:
                neg.append((a, bb))
            else:
                zero.append((a, bb))
        new = []
        for ap, bp in pos:
            for an, bn in neg:
                # ap/a[j] 给出 x_j <= ...; 归一化正行系数为 +1，负行为 -1，相加
                rp = tuple(v / ap[j] for v in ap)
                rn = tuple(v / an[j] for v in an)
                merged = tuple(
                    (rp[c] - rn[c]) for c in range(k) if c != j
                )
                new.append((merged, bp / ap[j] - bn / an[j]))
        rows = [(tuple(v for c, v in enumerate(a) if c != j), bb) for a, bb in zero] + new
        if len(rows) > 4000:  # 小规模 fuzz 的安全阀
            raise RuntimeError("FM blowup")
    return True


def test_fuzz_against_fourier_motzkin():
    import random
    rng = random.Random(20260929)
    trials = 400
    for t in range(trials):
        k = rng.randint(1, 3)
        m = rng.randint(1, 9)
        A = [[rng.randint(-2, 2) for _ in range(k)] for _ in range(m)]
        b = [rng.randint(-3, 3) for _ in range(m)]
        expect = _fm_feasible(list(zip([tuple(r) for r in A], b)))
        res = phase_one_simplex(A, b)
        if expect:
            assert res.status == "feasible", (A, b)
            _feasible_ok(A, b, res)
        else:
            assert res.status == "infeasible", (A, b)
            _cert_ok(A, b, res)



# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _payload(aid="a1", n=2, cons=None):
    if cons is None:
        cons = [{"coeffs": [1, 0], "rhs": 1, "stable": True}]
    return {"audit_id": aid, "variables": [{"name": f"x{j}"} for j in range(n)], "constraints": cons}


def test_validation_ok():
    aid, names, cons = validate_payload(_payload())
    assert aid == "a1" and names == ["x0", "x1"]
    assert cons[0].stable is True and cons[0].rhs == 1


def test_validation_rejects_float_bool_and_bad_shapes():
    bads = [
        (_payload(cons=[{"coeffs": [1.0, 0], "rhs": 1}]), "not_integer"),
        (_payload(cons=[{"coeffs": [True, 0], "rhs": 1}]), "not_integer"),
        (_payload(cons=[{"coeffs": [1], "rhs": 1}]), "bad_coeffs"),
        (_payload(aid="../etc"), "invalid_audit_id"),
        (_payload(aid=""), "invalid_audit_id"),
    ]
    for p, code in bads:
        try:
            validate_payload(p)
        except ValidationError as e:
            assert e.code == code, (e.code, code)
        else:
            raise AssertionError(f"should reject {p}")


def test_validation_limits():
    p = _payload(n=9)
    p["variables"] = [{"name": f"x{j}"} for j in range(9)]
    try:
        validate_payload(p)
    except ValidationError as e:
        assert e.code == "bad_variables"
    else:
        raise AssertionError("9 variables must be rejected")

    p = _payload(cons=[{"coeffs": [0, 0], "rhs": 0}] * 49)
    try:
        validate_payload(p)
    except ValidationError as e:
        assert e.code == "too_many_constraints"
    else:
        raise AssertionError("49 constraints must be rejected")


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def test_store_idempotent_and_conflict():
    with tempfile.TemporaryDirectory() as d:
        store = FrozenStore(d)
        p1 = _payload(cons=[{"coeffs": [1, 0], "rhs": 1}])
        p2 = _payload(cons=[{"coeffs": [1, 0], "rhs": 2}])  # 改载荷
        rec1, reused = store.submit("a1", p1, {"status": "feasible"}, "t1")
        assert reused is False
        rec1b, reused = store.submit("a1", p1, {"status": "feasible"}, "t2")
        assert reused is True
        assert rec1b["created_at"] == "t1"  # 时间戳也不覆盖
        assert rec1b["result"] == rec1["result"]
        assert store.get("a1")["result"] == {"status": "feasible"}

        try:
            store.submit("a1", p2, {"status": "infeasible"}, "t3")
        except ConflictError:
            pass
        else:
            raise AssertionError("different payload must conflict")
        # 原证据不变
        assert store.get("a1")["payload_sha256"] == canonical_hash(p1)
        assert store.get("a1")["result"] == {"status": "feasible"}


def test_canonical_hash_stable():
    p1 = {"audit_id": "a", "variables": [], "constraints": []}
    p2 = {"constraints": [], "audit_id": "a", "variables": []}  # 键序不同
    assert canonical_hash(p1) == canonical_hash(p2)
    p3 = {"audit_id": "a", "variables": [], "constraints": [{"coeffs": [], "rhs": 0}]}
    assert canonical_hash(p1) != canonical_hash(p3)


# ---------------------------------------------------------------------------
# HTTP 冒烟
# ---------------------------------------------------------------------------


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(method, url, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_smoke():
    port = _free_port()
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, PORT=str(port), HOST="127.0.0.1", DATA_DIR=d)
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.server"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    code, _ = _req("GET", base + "/healthz")
                    if code == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("server did not start")

            # 可行
            p = _payload("http-1", n=1, cons=[{"coeffs": [1], "rhs": 2}, {"coeffs": [-1], "rhs": 1}])
            p["variables"] = [{"name": "I1"}]
            code, rec = _req("POST", base + "/audits", p)
            assert code == 201
            assert rec["result"]["status"] == "feasible"

            # 幂等重传
            code, rec2 = _req("POST", base + "/audits", p)
            assert code == 200 and rec2["payload_sha256"] == rec["payload_sha256"]

            # 改载荷冲突
            p["constraints"][0]["rhs"] = 999
            code, err = _req("POST", base + "/audits", p)
            assert code == 409 and err["error"]["code"] == "conflict"
            # 原证据不变
            code, rec3 = _req("GET", base + "/audits/http-1")
            assert rec3["result"]["status"] == "feasible"
            assert rec3["payload_sha256"] == rec["payload_sha256"]

            # 非法输入不留痕
            bad = {"audit_id": "http-2", "variables": [{"name": "x"}],
                   "constraints": [{"coeffs": [1.5], "rhs": 0}]}
            code, err = _req("POST", base + "/audits", bad)
            assert code == 400
            code, _ = _req("GET", base + "/audits/http-2")
            assert code == 404

            # 0 <= -1 证书经 HTTP 核对
            cert_p = {"audit_id": "zero-neg-one", "variables": [{"name": "x"}],
                      "constraints": [{"coeffs": [0], "rhs": -1, "stable": True}]}
            code, rec = _req("POST", base + "/audits", cert_p)
            assert code == 201 and rec["result"]["status"] == "infeasible"
            c = rec["result"]["certificate"]
            assert c["combined_rhs"] == {"num": -1, "den": 1}
            assert c["multipliers"][0] == {"num": 1, "den": 1}
            assert c["combined_lhs"][0] == {"num": 0, "den": 1}
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nall {len(fns)} tests passed")
