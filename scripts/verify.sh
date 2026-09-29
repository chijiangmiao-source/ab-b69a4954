#!/bin/sh
# verify 单次容器入口：
#   1. 运行全部代码测试（纯标准库，无需 pytest）；
#   2. 对运行中的服务做 HTTP 冒烟并核对 0 <= -1 的不可行性证书；
#   3. 以核对脚本的退出码作为容器退出码。
set -eu

cd "${APP_DIR:-/app}"
export PYTHONPATH="${APP_DIR:-/app}"
PYTHON="${PYTHON:-python}"
BASE_URL="${BASE_URL:-http://api:8080}"

echo "== [1/2] 代码测试 =="
"$PYTHON" tests/test_all.py

echo "== [2/2] HTTP 冒烟与 Farkas 证书核对（${BASE_URL}） =="
exec "$PYTHON" scripts/verify_cert.py --base-url "${BASE_URL}"
