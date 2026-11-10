#!/usr/bin/env bash
# bootstrap-certs.sh — one-time device-certificate bootstrap for the
# USB-SOVEREIGN live AWS sinks. Runs ON THE EC2 HOST as the deploy user
# (invoked by deploy.sh only when USB_SOV_BOOTSTRAP=1; see
# aws-activation/README.md).
#
# What it does:
#   1. If the secrets dir already holds cert.pem + private.key +
#      AmazonRootCA1.pem + a cert-record.json with a certificateId,
#      exit 0 doing nothing (idempotent).
#   2. Generate the device private key + CSR LOCALLY with openssl.
#      The private key is created here and never leaves this host —
#      only the CSR (public material) goes to AWS.
#   3. Fetch instance-profile credentials from the metadata service
#      (IMDSv2) and, via the stdlib SigV4 signer in sigv4.py next to
#      this script, call IoT CreateCertificateFromCsr (setAsActive),
#      AttachPolicy (SovereignBridgePolicy) and AttachThingPrincipal
#      (SovereignBridge). Persist cert.pem + cert-record.json.
#   4. Download the Amazon Root CA 1 PEM from its public URL and record
#      the account's IoT data endpoint (iot-endpoint.txt) for Stage 3.
#
# Fails closed: any error exits non-zero BEFORE anything is half-done
# being treated as success; a partial run leaves no cert-record.json,
# so the next run retries cleanly (an orphaned ACTIVE certificate from
# a failed attach step is possible after a mid-run crash — the runbook
# has the console cleanup note for that case).
set -euo pipefail

SECRETS_DIR="${USB_SOV_SECRETS_DIR:-/home/deploy/live-servers/secrets/usb-sovereign}"
REGION="${USB_SOV_AWS_REGION:-us-west-2}"
THING_NAME="${USB_SOV_THING_NAME:-SovereignBridge}"
POLICY_NAME="${USB_SOV_IOT_POLICY_NAME:-SovereignBridgePolicy}"
ROOT_CA_URL="https://www.amazontrust.com/repository/AmazonRootCA1.pem"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CERT_PEM="${SECRETS_DIR}/cert.pem"
PRIVATE_KEY="${SECRETS_DIR}/private.key"
CA_PEM="${SECRETS_DIR}/AmazonRootCA1.pem"
RECORD="${SECRETS_DIR}/cert-record.json"
CSR_FILE="${SECRETS_DIR}/device.csr"
ENDPOINT_FILE="${SECRETS_DIR}/iot-endpoint.txt"

log() { echo "[bootstrap-certs] $*"; }

# --- 1. idempotency gate -------------------------------------------------
if [ -f "${CERT_PEM}" ] && [ -f "${PRIVATE_KEY}" ] && [ -f "${CA_PEM}" ] \
   && [ -f "${RECORD}" ] \
   && python3 -c "import json,sys; d=json.load(open('${RECORD}')); sys.exit(0 if d.get('certificateId') else 1)" 2>/dev/null; then
  log "existing certificate found in ${SECRETS_DIR} — nothing to do."
  exit 0
fi

mkdir -p "${SECRETS_DIR}"
chmod 700 "${SECRETS_DIR}"

# --- 2. private key + CSR (generated on this host, key never leaves) -----
if [ ! -f "${PRIVATE_KEY}" ]; then
  log "generating device private key (RSA 2048) in ${SECRETS_DIR}"
  umask 077
  openssl genrsa -out "${PRIVATE_KEY}" 2048 2>/dev/null
  chmod 600 "${PRIVATE_KEY}"
else
  log "private key already present — reusing it (CSR regenerated)."
fi
openssl req -new -key "${PRIVATE_KEY}" -out "${CSR_FILE}" \
  -subj "/CN=${THING_NAME}"
chmod 600 "${CSR_FILE}"

# --- 3. Amazon Root CA 1 --------------------------------------------------
if [ ! -f "${CA_PEM}" ]; then
  log "downloading Amazon Root CA 1"
  curl -fsSL "${ROOT_CA_URL}" -o "${CA_PEM}"
  chmod 600 "${CA_PEM}"
fi

# --- 4. AWS calls (stdlib SigV4; credentials from IMDS) -------------------
log "requesting certificate from CSR via AWS IoT (region ${REGION})"
USB_SOV_BOOTSTRAP_REGION="${REGION}" \
USB_SOV_BOOTSTRAP_THING="${THING_NAME}" \
USB_SOV_BOOTSTRAP_POLICY="${POLICY_NAME}" \
USB_SOV_BOOTSTRAP_CSR="${CSR_FILE}" \
USB_SOV_BOOTSTRAP_CERT_OUT="${CERT_PEM}" \
USB_SOV_BOOTSTRAP_RECORD_OUT="${RECORD}" \
USB_SOV_BOOTSTRAP_ENDPOINT_OUT="${ENDPOINT_FILE}" \
python3 - "${SCRIPT_DIR}" <<'PYEOF'
import json
import os
import sys
import urllib.request

sys.path.insert(0, sys.argv[1])  # sigv4.py sits next to this script
from sigv4 import default_credentials, sign_request  # noqa: E402

REGION = os.environ["USB_SOV_BOOTSTRAP_REGION"]
THING = os.environ["USB_SOV_BOOTSTRAP_THING"]
POLICY = os.environ["USB_SOV_BOOTSTRAP_POLICY"]
HOST = f"iot.{REGION}.amazonaws.com"

creds = default_credentials()  # IMDSv2 instance profile (env fallback)


def call(method, path, query="", body=b"", extra_headers=None):
    headers = sign_request(method, HOST, REGION, "iot", creds,
                           path=path, query=query,
                           headers=extra_headers or {}, body=body)
    url = f"https://{HOST}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, data=body if method != "GET" else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(
            f"[bootstrap-certs] IoT API {method} {path} failed: "
            f"HTTP {exc.code}: {detail}")


# CreateCertificateFromCsr — body is the CSR PEM; setAsActive=true.
with open(os.environ["USB_SOV_BOOTSTRAP_CSR"], "rb") as fh:
    csr_pem = fh.read()
status, text = call("POST", "/certificates", query="setAsActive=true",
                    body=csr_pem)
cert = json.loads(text)
cert_id = cert["certificateId"]
cert_arn = cert["certificateArn"]
with open(os.environ["USB_SOV_BOOTSTRAP_CERT_OUT"], "w") as fh:
    fh.write(cert["certificatePem"])
os.chmod(os.environ["USB_SOV_BOOTSTRAP_CERT_OUT"], 0o600)
print(f"[bootstrap-certs] certificate created: id={cert_id}")

# AttachPolicy + AttachThingPrincipal for the new certificate.
call("PUT", f"/policies/{POLICY}",
     body=json.dumps({"target": cert_arn}).encode(),
     extra_headers={"content-type": "application/json"})
print(f"[bootstrap-certs] policy attached: {POLICY}")
call("PUT", f"/things/{THING}/principals",
     extra_headers={"x-amz-principal": cert_arn})
print(f"[bootstrap-certs] thing principal attached: {THING}")

# DescribeEndpoint (data plane, ATS) — Stage 3 needs this host name.
status, text = call("GET", "/endpoint", query="endpointType=iot%3AData-ATS")
endpoint = json.loads(text)["endpointAddress"]
with open(os.environ["USB_SOV_BOOTSTRAP_ENDPOINT_OUT"], "w") as fh:
    fh.write(endpoint + "\n")

record = {
    "certificateId": cert_id,
    "certificateArn": cert_arn,
    "thingName": THING,
    "policyName": POLICY,
    "region": REGION,
    "iotDataEndpoint": endpoint,
}
with open(os.environ["USB_SOV_BOOTSTRAP_RECORD_OUT"], "w") as fh:
    json.dump(record, fh, indent=2)
os.chmod(os.environ["USB_SOV_BOOTSTRAP_RECORD_OUT"], 0o600)
print(f"[bootstrap-certs] recorded certificate {cert_id}; "
      f"IoT data endpoint: {endpoint}")
PYEOF

chmod 600 "${CERT_PEM}" "${RECORD}"
log "done. Certificate + key + CA in ${SECRETS_DIR} (dir 0700, files 0600)."
log "Stage 3: put USB_SOV_IOT_ENDPOINT=$(cat "${ENDPOINT_FILE}") in the host .env."
