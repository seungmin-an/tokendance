#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(dirname "$HERE")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# seed a ledger with a playbook entry
mkdir -p "$TMP/library"
cat > "$TMP/library/.harvest-ledger.json" <<'JSON'
{"version":1,"entries":{"playbook:pb:x":{"title":"PB X","scope":"playbook","repo":null,"slug":"x","dest":"playbooks/x.md","summary":"do X","tags":"","body":"b","tier":"primary","sources":["t1"]}}}
JSON
BLOCK="$(python3 "$ROOT/scripts/harvest_knowledge.py" --root "$TMP" --recall /any/repo)"
echo "$BLOCK" | grep -q "PB X" || { echo "FAIL: recall block missing entry"; exit 1; }
# empty ledger → empty block, exit 0
echo '{"version":1,"entries":{}}' > "$TMP/library/.harvest-ledger.json"
EMPTY="$(python3 "$ROOT/scripts/harvest_knowledge.py" --root "$TMP" --recall /any/repo)"
[ -z "$EMPTY" ] || { echo "FAIL: expected empty recall"; exit 1; }
echo "PASS"
