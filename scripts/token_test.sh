TOKEN=$(curl -s -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username":"dashboard","password":""}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

endpoints=(
  "GET /smartgate/status"
  "GET /logs/stats"
  "GET /logs/categories"
  "GET /smartgate/registered-faces"
)

echo "=== 토큰 없이 (401 출력이면 정상 ) ==="
for ep in "${endpoints[@]}"; do
  method=$(echo $ep | cut -d' ' -f1)
  path=$(echo $ep | cut -d' ' -f2)
  code=$(curl -s -o /dev/null -w "%{http_code}" -X $method http://localhost:8000$path)
  echo "$code $ep"
done

echo ""
echo "=== 토큰 포함 (200 출력이면 정상 ) ==="
for ep in "${endpoints[@]}"; do
  method=$(echo $ep | cut -d' ' -f1)
  path=$(echo $ep | cut -d' ' -f2)
  code=$(curl -s -o /dev/null -w "%{http_code}" -X $method \
    -H "Authorization: Bearer $TOKEN" http://localhost:8000$path)
  echo "$code $ep"
done
