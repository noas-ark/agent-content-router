#!/usr/bin/env bash
# test_sse.sh — verify SSE keepalives and final data line arrive correctly.
#
# Usage:
#   ./test_sse.sh [query] [host]
#
# Examples:
#   ./test_sse.sh                                    # default query, localhost:5001
#   ./test_sse.sh "impact of tariffs on supply chain"
#   ./test_sse.sh "impact of tariffs" https://bootk.onrender.com
#
# What it checks:
#   1. Keepalive comments (": keepalive") arrive before the pipeline finishes
#   2. A "data: " line eventually arrives
#   3. The JSON in the data line parses cleanly

QUERY="${1:-what are the latest SEC enforcement actions against DeFi protocols in 2025}"
HOST="${2:-http://localhost:5001}"

echo "====================================================="
echo "  SSE test"
echo "  Host : $HOST"
echo "  Query: $QUERY"
echo "====================================================="
echo ""

START=$(date +%s)
KEEPALIVES=0
GOT_DATA=0
DATA_LINE=""

while IFS= read -r line; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))

  if [[ "$line" == :* ]]; then
    KEEPALIVES=$((KEEPALIVES + 1))
    echo "[${ELAPSED}s] keepalive #${KEEPALIVES}"
  elif [[ "$line" == data:* ]]; then
    GOT_DATA=1
    DATA_LINE="${line:5}"   # strip "data:"
    echo "[${ELAPSED}s] data line received (${#DATA_LINE} bytes)"
    break
  fi
done < <(curl -sN --no-buffer \
  -X POST "$HOST/optimize" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"$QUERY\", \"customer_id\": \"test\"}" \
  2>&1)

echo ""
echo "====================================================="
TOTAL=$(($(date +%s) - START))
echo "  Elapsed    : ${TOTAL}s"
echo "  Keepalives : $KEEPALIVES"

if [[ $GOT_DATA -eq 0 ]]; then
  echo "  RESULT     : FAIL — no data line received"
  exit 1
fi

# Try to parse the JSON
if python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    ok = d.get('ok', True)
    err = d.get('error', '')
    subs = len(d.get('sub_query_runs', []))
    cov = d.get('coverage_summary', '')
    print(f'  ok         : {ok}')
    if err:
        print(f'  error      : {err}')
    print(f'  sub-queries: {subs}')
    if cov:
        print(f'  coverage   : {cov}')
    if ok and not err:
        print('  RESULT     : PASS')
        sys.exit(0)
    else:
        print('  RESULT     : FAIL — pipeline error')
        sys.exit(1)
except Exception as e:
    print(f'  RESULT     : FAIL — JSON parse error: {e}')
    sys.exit(1)
" "$DATA_LINE"; then
  exit 0
else
  exit 1
fi
