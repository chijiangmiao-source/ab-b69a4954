"""verify 单次容器的 HTTP 冒烟与 Farkas 证书核对。

核对内容（全部在真实 HTTP 链路上进行）：

1. ``GET /healthz`` 返回 200；
2. 提交"一组相加得 0 <= -1"的约束（x <= 0 与 -x <= -1，乘子各 1），
   服务必须返回不可行，且非负有理乘子使左侧相加恰为 0、右侧和恰为 -1，
   并逐项给出加权合并式；
3. 按审计编号重新读取，得到同一条冻结记录；
4. 同编号同载荷重传幂等；
5. 同编号改载荷冲突 409，且原证据不变；
6. 非法输入 400 且不残留记录。

任一步失败即以非零退出码退出。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _req(method: str, url: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _frac(d) -> int:
    return int(d["num"]), int(d["den"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://api:8080")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    # 1) 健康检查
    code, _ = _req("GET", base + "/healthz")
    check("GET /healthz -> 200", code == 200, f"got {code}")

    # 2) 一组相加得 0 <= -1 的约束：x <= 0；-x <= -1。
    payload = {
        "audit_id": "verify-zero-le-minus-one",
        "variables": [{"name": "I1"}],
        "constraints": [
            {"coeffs": [1], "rhs": 0, "stable": True},
            {"coeffs": [-1], "rhs": -1, "stable": True},
        ],
    }
    code, rec = _req("POST", base + "/audits", payload)
    check("POST 矛盾约束 -> 201", code == 201, f"got {code}")
    if code == 201:
        result = rec["result"]
        check("状态为 infeasible", result.get("status") == "infeasible")
        cert = result.get("certificate", {})
        mults = cert.get("multipliers", [])
        check("乘子数量为 2", len(mults) == 2)
        if len(mults) == 2:
            m1 = _frac(mults[0])
            m2 = _frac(mults[1])
            check("乘子均非负", m1[0] >= 0 and m2[0] >= 0 and m1[1] > 0 and m2[1] > 0,
                  f"{m1[0]}/{m1[1]}, {m2[0]}/{m2[1]}")
            # 两条约束的乘子必须相等（合并 x 抵消）；允许任意正整数倍，
            # 这里服务以 Bland 固定枢轴给出 (1,1)。
            check(
                "乘子为 1 与 1（相加抵消）",
                m1 == (1, 1) and m2 == (1, 1),
                f"{m1}, {m2}",
            )
        lhs = cert.get("combined_lhs", [])
        check("合并左侧 = [0]", len(lhs) == 1 and _frac(lhs[0]) == (0, 1))
        check("合并右侧 = -1", _frac(cert.get("combined_rhs", {})) == (-1, 1))
        terms = cert.get("terms", [])
        check("逐项合并式列出 2 条", len(terms) == 2)
        if len(terms) == 2:
            wlhs = [_frac(t["weighted_coeffs"][0]) for t in terms]
            wrhs = [_frac(t["weighted_rhs"]) for t in terms]
            check(
                "逐项加权系数 (1,0) 与 (-1,0)，相加为 0",
                sorted(wlhs) == sorted([(1, 1), (-1, 1)]),
                str(wlhs),
            )
            check(
                "逐项加权右侧为 0 与 -1，相加为 -1",
                sorted(wrhs) == sorted([(0, 1), (-1, 1)]),
                str(wrhs),
            )

    # 3) 按编号重新读取冻结结果
    code, got = _req("GET", base + "/audits/verify-zero-le-minus-one")
    check("GET 冻结记录 -> 200", code == 200)
    if code == 200:
        same = got.get("payload_sha256") == rec.get("payload_sha256")
        check("读回与提交为同一记录", same)

    # 4) 同编号完全相同载荷重传 -> 200 幂等
    code, again = _req("POST", base + "/audits", payload)
    check("同载荷重传 -> 200 幂等", code == 200, f"got {code}")
    if code == 200:
        check(
            "幂等返回同一记录",
            again.get("payload_sha256") == rec.get("payload_sha256"),
        )

    # 5) 同编号改动载荷 -> 409，且原证据不变
    changed = json.loads(json.dumps(payload))
    changed["constraints"][1]["rhs"] = 0
    code, err = _req("POST", base + "/audits", changed)
    check("改动载荷复用编号 -> 409", code == 409, f"got {code}")
    code, frozen = _req("GET", base + "/audits/verify-zero-le-minus-one")
    unchanged = (
        code == 200
        and frozen.get("result", {}).get("status") == "infeasible"
        and frozen.get("payload_sha256") == rec.get("payload_sha256")
    )
    check("冲突后原证据不变", unchanged)

    # 6) 非法输入不残留
    bad = {
        "audit_id": "verify-invalid",
        "variables": [{"name": "I1"}],
        "constraints": [{"coeffs": [1.5], "rhs": 0}],  # 非整数
    }
    code, _ = _req("POST", base + "/audits", bad)
    check("非法输入 -> 400", code == 400, f"got {code}")
    code, _ = _req("GET", base + "/audits/verify-invalid")
    check("非法输入不留存任何记录", code == 404, f"got {code}")

    if failures:
        print(f"\nVERIFY FAILED: {len(failures)} check(s): {failures}", file=sys.stderr)
        return 1
    print("\nVERIFY OK: certificate, freeze semantics and HTTP smoke all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
