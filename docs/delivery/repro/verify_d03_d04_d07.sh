#!/usr/bin/env bash
# 复现脚本：D-03 / D-04 / D-07 全部结论。
# 用法：cd /root/DSH/应用/ssqyuce && bash docs/delivery/repro/verify_d03_d04_d07.sh
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 1
PY="$PWD/.venv/bin/python"

echo "=========== 1. 五个锁定常量（实时读取）==========="
$PY -c "
from lottery import routes_capabilities as C
b = C.capabilities()
for k, v in b['constants'].items():
    print(f'  {k:12s} = {v!r}')
print('  sources:', b['constants_meta']['sources'])
"

echo
echo "=========== 2. cannot 段四条硬否定 ==========="
$PY -c "
from lottery import routes_capabilities as C
for i in C.capabilities()['cannot']:
    print(f'  [{i[\"id\"]}] {i[\"title\"]}')
"

echo
echo "=========== 3. 当前降级项（真实状态）==========="
$PY -c "
from lottery import routes_capabilities as C
for i in C.capabilities()['degraded_items']:
    print(f'  [{i[\"id\"]}] {i[\"how_to_restore\"]}')
"

echo
echo "=========== 4. D-07 usable_semantics ==========="
$PY -c "
from lottery import idle_mining as I
s = I.status()['usable_semantics']
print('  authoritative:', s['this_module_authoritative_field'])
print('  collides_with:', s['collides_with']['field'])
print('  never_show_together:', s['never_show_together'])
print('  rename_plan:', s['rename_plan'])
"

echo
echo "=========== 5. 三段测试 ==========="
$PY -m pytest tests/test_d03_boundary_hardening.py tests/test_d04_capabilities_api.py tests/test_d07_usable_semantics.py -q 2>&1 | tail -3
