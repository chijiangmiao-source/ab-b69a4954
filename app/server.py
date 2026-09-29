"""审计 HTTP 服务（仅依赖 Python 标准库）。

路由：

* ``GET  /``            浏览器录入/查询页面
* ``GET  /healthz``     健康检查
* ``POST /audits``      提交审计（幂等；载荷冲突返回 409）
* ``GET  /audits/{id}`` 按审计编号读取冻结结果

宿主机/端口可通过环境变量 ``HOST``、``PORT`` 配置；证据目录由
``DATA_DIR`` 配置。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .simplex import Feasible, feasible_payload, infeasible_payload, phase_one_simplex
from .storage import ConflictError, FrozenStore, RecordNotFound
from .validation import ValidationError, validate_payload

_STORE_LOCK = threading.Lock()
STORE: FrozenStore | None = None

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>低温磁阱电流安全审计</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 system-ui, sans-serif; max-width: 1080px; margin: 1.5rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; }
  fieldset { margin: .8rem 0; padding: .8rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #888; padding: .25rem .4rem; text-align: left; }
  input[type=number] { width: 7em; }
  input[type=text] { width: 12em; }
  .mono, td.num { font-family: ui-monospace, Menlo, Consolas, monospace; }
  .ok { color: #0a7d28; font-weight: 700; }
  .bad { color: #b00020; font-weight: 700; }
  button { margin: .4rem .4rem .4rem 0; padding: .35rem .9rem; }
  pre { background: rgba(127,127,127,.12); padding: .6rem; overflow:auto; }
  .muted { color: #777; }
</style>
</head>
<body>
<h1>低温磁阱电流安全审计（任意精度有理数 Phase-I 单纯形 / Bland）</h1>

<form id="f" onsubmit="return false;">
  <fieldset>
    <legend>审计</legend>
    <label>审计编号 <input type="text" id="audit_id" value="run-001" required></label>
    <label>变量个数 <input type="number" id="nvars" min="1" max="8" value="2"></label>
    <label>约束条数 <input type="number" id="ncons" min="0" max="48" value="3"></label>
    <button id="rebuild">重建表格</button>
  </fieldset>
  <fieldset>
    <legend>变量名（逗号分隔，可留空）</legend>
    <input type="text" id="names" style="width:100%" placeholder="例如 I1, I2">
  </fieldset>
  <fieldset>
    <legend>约束 a·x ≤ b（整数系数；勾选项为稳定标识）</legend>
    <table id="ctable"><thead></thead><tbody></tbody></table>
  </fieldset>
  <button id="submit">提交审计</button>
  <button id="fetch">按编号读取冻结结果</button>
</form>

<h2>结果</h2>
<div id="status" class="muted">尚未提交。</div>
<div id="result"></div>

<script>
const $ = (id) => document.getElementById(id);
function rebuild() {
  const n = Math.max(1, Math.min(8, +$("nvars").value || 1));
  const m = Math.max(0, Math.min(48, +$("ncons").value || 0));
  $("nvars").value = n; $("ncons").value = m;
  const names = $("names").value.split(",").map(s => s.trim()).filter(Boolean);
  const head = $("ctable").querySelector("thead");
  const body = $("ctable").querySelector("tbody");
  head.innerHTML = "<tr><th>#</th>" +
    Array.from({length:n},(_,j)=>`<th>${names[j]||("x"+(j+1))} 系数</th>`).join("") +
    "<th>b（右侧）</th><th>稳定</th></tr>";
  body.innerHTML = Array.from({length:m},(_,i)=>`<tr><td>${i+1}</td>` +
    Array.from({length:n},()=>`<td><input type="number" step="1" class="coeff" value="0"></td>`).join("") +
    `<td><input type="number" step="1" class="rhs" value="0"></td>
     <td><input type="checkbox" class="stable"></td></tr>`).join("");
}
$("rebuild").onclick = rebuild;
$("nvars").onchange = rebuild; $("ncons").onchange = rebuild; $("names").onchange = rebuild;
rebuild();

function frac(f) { return `<span class="num">${f.num}/${f.den}</span>` + (f.den === 1 ? "" : ` <span class="muted">(≈ ${(Number(f.num)/Number(f.den)).toFixed(6)})</span>`); }
function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function render(rec) {
  const r = rec.result, p = rec.payload;
  const names = (p.variables || []).map(v => typeof v === "string" ? v : v.name);
  $("status").innerHTML = rec.reused
    ? `<span class="muted">复用冻结记录 ${esc(rec.audit_id)}（载荷 SHA-256: ${rec.payload_sha256.slice(0,16)}…，创建于 ${esc(rec.created_at)}）</span>`
    : `<span class="muted">已冻结记录 ${esc(rec.audit_id)}（载荷 SHA-256: ${rec.payload_sha256.slice(0,16)}…，创建于 ${esc(rec.created_at)}）</span>`;
  let h = "";
  if (r.status === "feasible") {
    h += `<p class="ok">可行：以下有理电流同时满足全部约束（精确余量 ≥ 0）。</p><table>
      <tr><th>变量</th><th>有理取值</th></tr>` +
      r.solution.map((v,j)=>`<tr><td>${esc(names[j]||("x"+(j+1)))}</td><td>${frac(v)}</td></tr>`).join("") +
      `</table><h3>每条约束的精确余量</h3><table><tr><th>#</th><th>余量 b−a·x</th><th>是否起作用</th><th>稳定标识</th></tr>` +
      r.constraints.map((c,i)=>`<tr><td>${i+1}</td><td>${frac(c.slack)}</td>
        <td>${c.binding ? "是（等式起作用）" : "否"}</td><td>${c.stable ? "稳定" : ""}</td></tr>`).join("") +
      `</table>`;
  } else {
    const cert = r.certificate;
    h += `<p class="bad">无解：Farkas 证书给出非负有理乘子，使左侧相加为零、右侧和严格为负。</p>
      <table><tr><th>#</th><th>乘子 π（非负）</th><th>加权左侧系数</th><th>加权右侧</th><th>原行方向</th></tr>` +
      cert.terms.map(t => `<tr><td>${t.index+1}</td><td>${frac(t.multiplier)}</td>
        <td>[${t.weighted_coeffs.map(frac).join(", ")}]</td><td>${frac(t.weighted_rhs)}</td>
        <td>${t.orientation === -1 ? "该行整体翻转过(×−1)" : "原向"}</td></tr>`).join("") +
      `</table><p>合并结果：左侧 = [${cert.combined_lhs.map(frac).join(", ")}] = 0，
      右侧和 = ${frac(cert.combined_rhs)} &lt; 0，即 <b>0 ≤ ${frac(cert.combined_rhs)}</b>，矛盾。</p>`;
  }
  $("result").innerHTML = h;
}

async function submit() {
  const n = +$("nvars").value;
  const names = $("names").value.split(",").map(s=>s.trim());
  const body = $("ctable").querySelector("tbody");
  const constraints = [...body.rows].map(row => ({
    coeffs: [...row.querySelectorAll("input.coeff")].map(e => parseInt(e.value, 10) || 0),
    rhs: parseInt(row.querySelector("input.rhs").value, 10) || 0,
    stable: row.querySelector("input.stable").checked
  }));
  const payload = {
    audit_id: $("audit_id").value,
    variables: Array.from({length:n},(_,j)=>({name: names[j] || ("x"+(j+1))})),
    constraints
  };
  const res = await fetch("/audits", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
  const data = await res.json();
  if (!res.ok) { $("status").innerHTML = `<span class="bad">${res.status}：${esc(data.error&&data.error.message || JSON.stringify(data))}</span>`; $("result").textContent=""; return; }
  render(data);
}
$("submit").onclick = submit;
$("fetch").onclick = async () => {
  const res = await fetch("/audits/" + encodeURIComponent($("audit_id").value));
  const data = await res.json();
  if (!res.ok) { $("status").innerHTML = `<span class="bad">${res.status}：${esc(data.error&&data.error.message || JSON.stringify(data))}</span>`; $("result").textContent=""; return; }
  data.reused = true; render(data);
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "TrapAudit/1.0"

    def _send_json(self, code: int, obj: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, err_code: str, message: str) -> None:
        self._send_json(code, {"error": {"code": err_code, "message": message}})

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/" or path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/audits/"):
            audit_id = unquote(path[len("/audits/") :])
            if not audit_id or "/" in audit_id:
                self._error(404, "not_found", "记录不存在")
                return
            record = STORE.get(audit_id)
            if record is None:
                self._error(404, "not_found", f"审计编号 {audit_id!r} 不存在")
                return
            self._send_json(200, record)
            return
        self._error(404, "not_found", "未知路径")

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/audits":
            self._error(404, "not_found", "未知路径")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4 * 1024 * 1024:
            self._error(400, "bad_request", "请求体缺失或超过 4 MiB 上限")
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid_json", "请求体不是合法的 UTF-8 JSON")
            return
        try:
            audit_id, names, constraints = validate_payload(payload)
        except ValidationError as e:
            # 非法输入：直接拒绝，不触碰存储，不会残留任何旧结论。
            self._error(400, e.code, e.message)
            return

        A = [list(c.coeffs) for c in constraints]
        b = [c.rhs for c in constraints]
        try:
            res = phase_one_simplex(A, b)
        except Exception as e:  # pragma: no cover - 断言/不变量失败
            self._error(500, "solver_failure", f"求解器内部错误: {e!r}")
            return

        if isinstance(res, Feasible):
            result = feasible_payload(res, constraints)
        else:
            result = infeasible_payload(res)
        result["variable_names"] = names
        result["algorithm"] = {
            "method": "phase-I simplex, exact rational arithmetic (fractions.Fraction)",
            "pivot_rule": "Bland",
            "free_variable_encoding": "x_j = p_j - n_j, p_j,n_j >= 0",
            "floating_point_used": False,
        }

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            record, reused = STORE.submit(audit_id, payload, result, now)
        except ConflictError as e:
            self._send_json(
                409,
                {
                    "error": {
                        "code": "conflict",
                        "message": (
                            "审计编号已存在但载荷不同；原冻结证据保持不变。"
                            f" 既有载荷 {e.existing_hash[:16]}…，"
                            f"本次载荷 {e.incoming_hash[:16]}…"
                        ),
                        "existing_payload_sha256": e.existing_hash,
                        "incoming_payload_sha256": e.incoming_hash,
                    }
                },
            )
            return
        self._send_json(200 if reused else 201, record, {"X-Audit-Reused": "1" if reused else "0"})


def build_server(host: str, port: int, data_dir: str) -> ThreadingHTTPServer:
    global STORE
    STORE = FrozenStore(data_dir)
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="低温磁阱电流审计服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    args = parser.parse_args(argv)
    httpd = build_server(args.host, args.port, args.data_dir)
    print(f"audit server listening on http://{args.host}:{args.port} (data={args.data_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
