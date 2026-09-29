# 低温磁阱电流安全审计服务

在调整多个线圈电流前，对形如 `a·x ≤ b`（整数系数）的安全 / 电源 / 场强
线性上界做**可行性审计**。后端用**任意精度有理数**（`fractions.Fraction`）
执行 **Phase-I 单纯形 + Bland 枢轴规则**，不使用浮点、采样或任何外部 LP
服务。

- 可行：返回由固定枢轴规则得到的一组**有理电流**，以及每条约束的**精确余量**。
- 无解：返回 **Farkas 证书**——非负有理乘子 π，使
  `Σ π_i·a_i = 0` 且 `Σ π_i·b_i < 0`（即合并出 `0 ≤ 负数`），
  并逐项展示加权合并式。

## 目录

```
app/simplex.py    精确有理数 Phase-I 单纯形 + Bland 规则 + Farkas 证书
app/validation.py 输入校验（1–8 变量，≤48 约束，严格整数，稳定标识）
app/storage.py    冻结证据存储（原子写入、幂等、冲突保护）
app/server.py     标准库 HTTP 服务 + 浏览器录入页面
tests/test_all.py 20 项测试（含 Fourier–Motzkin 独立 oracle 随机交叉验证）
scripts/          verify 单次容器入口与 HTTP/证书核对
Dockerfile        镜像（内置 /healthz 健康检查）
docker-compose.yml api 服务 + 单次 verify 容器
```

## 本地运行（无需 Docker）

```bash
pip install 无第三方依赖，Python ≥ 3.11 即可
HOST=127.0.0.1 PORT=8080 DATA_DIR=./data python -m app.server
# 浏览器打开 http://127.0.0.1:8080
```

运行测试 / 核对：

```bash
python tests/test_all.py
APP_DIR=$PWD PYTHON=python3 BASE_URL=http://127.0.0.1:8080 sh scripts/verify.sh
```

## Compose 运行

宿主机端口可配置（`.env` 或环境变量）：

```bash
cp .env.example .env        # 可改 HOST_PORT / HOST_BIND
docker compose build
docker compose up           # 前台运行可直接看到 verify 输出
# 或后台：
docker compose up -d
docker compose ps           # api 为 healthy；verify 为 Exited (0)
docker compose logs verify  # 查看单次核对结果与退出码
```

`verify` 是**单次容器**：等 `api` 健康后依次完成

1. 全部代码测试；
2. HTTP 冒烟（健康检查、提交、冻结读取、幂等、409 冲突、非法输入 400 不留痕）；
3. 用一组相加得 `0 ≤ -1` 的约束（`x ≤ 0` 与 `-x ≤ -1`，乘子各 1）
   核对不可行性证书的每一项；

完成后退出，退出码即结论（`docker inspect ... --format '{{.State.ExitCode}}'`
应为 0）。镜像构建由 `docker compose build` / `up` 自动完成。

## HTTP API

### `POST /audits`

```json
{
  "audit_id": "run-001",
  "variables": [{"name": "I1"}, {"name": "I2"}],
  "constraints": [
    {"coeffs": [1, 1], "rhs": 3, "stable": true},
    {"coeffs": [-1, 0], "rhs": 0}
  ]
}
```

- 首次提交：`201`；相同 `audit_id` + **完全相同载荷**重传：`200`，
  返回同一条冻结记录（响应头 `X-Audit-Reused: 1`）；
- 相同 `audit_id` + 不同载荷：`409`，**原有冻结证据保持不变**；
- 任何非法输入（非整数、越界、编号非法等）：`400`，不写入任何记录。

可行结果片段：

```json
{"status": "feasible",
 "solution": [{"num": 0, "den": 1}],
 "constraints": [{"index": 0, "slack": {"num": 1, "den": 2}, "binding": false, "stable": true}]}
```

无解结果片段（`0 ≤ -1`）：

```json
{"status": "infeasible",
 "certificate": {
   "multipliers": [{"num": 1, "den": 1}, {"num": 1, "den": 1}],
   "combined_lhs": [{"num": 0, "den": 1}],
   "combined_rhs": {"num": -1, "den": 1},
   "terms": [
     {"index": 0, "multiplier": {"num": 1, "den": 1}, "orientation": 1,
      "weighted_coeffs": [{"num": 1, "den": 1}], "weighted_rhs": {"num": 0, "den": 1}},
     {"index": 1, "multiplier": {"num": 1, "den": 1}, "orientation": 1,
      "weighted_coeffs": [{"num": -1, "den": 1}], "weighted_rhs": {"num": -1, "den": 1}}]}}
```

### `GET /audits/{audit_id}`

按审计编号重新读取冻结结果（载荷、SHA-256、创建时间、证据一并返回）。

### `GET /healthz`

返回 `{"status":"ok"}`，供 Docker / Compose 健康检查使用。

## 算法与正确性要点

- 自由电流变量编码为 `x_j = p_j − n_j`（p,n ≥ 0）；每条不等式加松弛变量；
  `b_i < 0` 的行整体翻转，再各加一个人工变量得到合法初始基；
  最小化人工变量之和 `w`。
- 入基按固定列序取首个正既约费用；比值平手取基变量下标最小者——
  **Bland 规则**保证退化时不循环。
- 表上拼接对初始行的凭证列，枢轴时同步做行变换；`w* = 0` 时读出可行解与
  精确余量，`w* > 0` 时由目标行凭证导出非负乘子 π，代码内有
  `Σπa=0`、`Σπb=−w*<0`、`π≥0` 的**精确断言**。
- 测试用独立实现的 Fourier–Motzkin 消元做 oracle，对数千个随机问题交叉
  验证可行/不可行结论，并对每份证书独立重新验算。
