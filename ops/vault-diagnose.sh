#!/usr/bin/env bash
# One-purpose diagnostic for the SecureStore receipt vault: why does
# /ledger/health report securestore_enabled:false after the key
# delivery + enable steps succeeded? Reports ONLY non-secret facts:
# presence of the two PAYLEDGER_SECURESTORE_* entries (host .env and
# inside the live container), the key's character length (a Fernet
# key is 44 chars — the value itself is never printed), the flag
# value, XDG_DATA_HOME, /data writability, and the vault module's own
# status inside the container, including its stderr reason line.
set -u
cd "$(dirname "$0")/.."

echo "=== HOST .env ==="
for name in PAYLEDGER_SECURESTORE_ENABLED PAYLEDGER_SECURESTORE_KEY; do
  line="$(grep -E "^${name}=" .env 2>/dev/null | tail -1 || true)"
  if [ -z "${line}" ]; then
    echo "${name}: ABSENT"
  elif [ "${name}" = "PAYLEDGER_SECURESTORE_ENABLED" ]; then
    echo "${name}: present, value '${line#*=}'"
  else
    v="${line#*=}"
    v="$(printf '%s' "${v}" | tr -d '[:space:]')"
    echo "${name}: present, length ${#v} (expect 44)"
  fi
done

echo "=== CONTAINER: env ==="
docker exec live-payledger python3 - << 'PYEOF' 2>&1 || echo "container python probe FAILED"
import os
key = os.environ.get("PAYLEDGER_SECURESTORE_KEY", "")
print("flag:", repr(os.environ.get("PAYLEDGER_SECURESTORE_ENABLED")))
print("key present:", bool(key.strip()), "length:", len(key.strip()), "(expect 44)")
print("XDG_DATA_HOME:", repr(os.environ.get("XDG_DATA_HOME")))
PYEOF

echo "=== CONTAINER: /data ==="
docker exec live-payledger sh -c 'ls -la /data 2>&1; touch /data/.write-test 2>&1 && echo "data writable" && rm /data/.write-test' || true

echo "=== CONTAINER: vault module ==="
docker exec live-payledger python3 - << 'PYEOF' 2>&1 || echo "vault probe FAILED"
from app_pkg import vault
print("vault_enabled():", vault.vault_enabled())
store = vault._get_store()
print("store constructed:", store is not None)
if store is not None:
    print("data_dir:", store.data_dir)
PYEOF

echo "=== CONTAINER LOGS (vault lines) ==="
docker logs live-payledger 2>&1 | grep -i 'vault' | tail -5 || echo "(no vault lines in logs)"

echo "=== DIAGNOSE DONE ==="
