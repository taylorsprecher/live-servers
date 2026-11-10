# USB-SOVEREIGN — AWS sink activation runbook

Activates the live AWS sinks for the USB-SOVEREIGN serial bridge, per
the plan Taylor approved on 2026-11-10. **Stage 1 (preparation) is
done**: the code, template, bootstrap, and tests in this folder and in
`backend/` are built and locally verified. Stages 2–4 below are the
operator steps, in order. Nothing in Stage 1 touched AWS.

## What gets created (Stage 2, `template.yaml`)

Stack `usb-sovereign-sinks`, region **us-west-2**, all names fixed:

| Resource | Name | Notes |
|---|---|---|
| S3 bucket | `911transedu-serial-batches` | private, SSE-S3, no versioning/lifecycle; receives `logs/serial_batch_<ts>.txt` flushes |
| DynamoDB table | `serial_events` | on-demand; single string partition key `pk` (see note below) |
| Lambda | `serial-events-writer` | python3.12, inline code; role may only `dynamodb:PutItem` on this table |
| IoT Thing | `SovereignBridge` | certificate attached later by the host bootstrap |
| IoT Policy | `SovereignBridgePolicy` | connect only as client `911TransEdu_SovereignBridge`; publish (and subscribe/receive) only on `device/serial/v2/data` |
| IoT Topic Rule | `serial_v2_to_lambda` | `SELECT * FROM 'device/serial/v2/data'` → the Lambda |
| IAM role + instance profile | `usb-sovereign-host` | steady state: `s3:PutObject` on the bucket only; plus one-time IoT bootstrap permissions (removable, see Security) |

Two code-vs-plan reconciliations baked into the template (from reading
the bridge code, which governs):

* **DynamoDB key**: the plan's shorthand was "source + timestamp"; the
  code (`sinks.DynamoDbStubSink`) keys items by the single composite
  string `source#timestamp` (e.g. `COM1#1794335019.42`). The table
  therefore has ONE partition key, `pk`, holding that composite;
  `source` and `timestamp` are also stored as attributes per item.
* **Bucket name**: the approved plan's `911transedu-serial-batches`
  wins. The stub-era constant `911transedu-telemetry-bucket` in
  `sinks.py` was only ever the local stubs' label; the live config
  default now matches the plan.

The device certificate is deliberately **not** in the template: the
host generates its private key locally and obtains the certificate via
`CreateCertificateFromCsr`, so key material never leaves the host and
never passes through CloudFormation, git, or chat.

## Stage 2 — create the AWS resources (CloudShell)

Run in Taylor's signed-in AWS CloudShell (browser). Get
`template.yaml` onto the CloudShell machine first (CloudShell's
Actions → Upload file is the simplest route; the live-servers repo is
private).

```bash
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name usb-sovereign-sinks \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-west-2

# Read the outputs (bucket, table, thing, policy, profile, Lambda ARN):
aws cloudformation describe-stacks \
  --stack-name usb-sovereign-sinks --region us-west-2 \
  --query 'Stacks[0].Outputs'

# The account's IoT data endpoint (needed in Stage 3):
aws iot describe-endpoint --endpoint-type iot:Data-ATS --region us-west-2

# Attach the instance profile to the audited host:
PROFILE_ARN=$(aws iam get-instance-profile \
  --instance-profile-name usb-sovereign-host \
  --query 'InstanceProfile.Arn' --output text)
aws ec2 modify-instance-attribute \
  --instance-id i-02d813192f607a4ea \
  --instance-profile Arn=$PROFILE_ARN,Name=usb-sovereign-host \
  --region us-west-2
```

Wait for `CREATE_COMPLETE` before continuing. (An instance-profile
swap on a running instance takes effect within a minute or two; no
reboot needed.)

## Stage 3 — host wiring and enablement

Order matters: **bootstrap first** (creates the cert), **then** set the
endpoint + flags, **then** redeploy.

1. Push the live-servers repo and run the normal deploy once. This
   lands the new code and compose wiring with every gate still OFF —
   verify `http://52.41.84.116:443/usb/api/status` still shows
   `"iot_sink": "MqttIoTStubSink"` (no behavior change yet).
2. SSH to the host as `deploy` and run the bootstrap once:

   ```bash
   cd /home/deploy/live-servers
   USB_SOV_BOOTSTRAP=1 bash deploy.sh
   ```

   Expect `[bootstrap-certs] certificate created: id=...`, the two
   attach lines, and the recorded IoT data endpoint. The private key,
   CSR, cert, Root CA, and `cert-record.json` now sit in
   `/home/deploy/live-servers/secrets/usb-sovereign/` (dir 0700,
   files 0600). Re-running is a no-op (idempotent).
3. Append to `/home/deploy/live-servers/.env` (values are hostnames
   and names only — no secrets):

   ```
   USB_SOV_AWS_ENABLED=1
   USB_SOV_AWS_IOT=1
   USB_SOV_AWS_S3=1
   USB_SOV_AWS_REGION=us-west-2
   USB_SOV_S3_BUCKET=911transedu-serial-batches
   USB_SOV_IOT_ENDPOINT=<contents of secrets/usb-sovereign/iot-endpoint.txt>
   ```

4. Redeploy (workflow dispatch, or `bash deploy.sh` on the host).
   Expected container log lines:

   ```
   [MQTT-AWS] Connected client_id=911TransEdu_SovereignBridge endpoint=<...> topic=device/serial/v2/data
   [AWS] AwsIoTClient armed — live IoT publishing to AWS IoT Core ...
   [AWS] AwsS3BatchLogger armed — live S3 archival to s3://911transedu-serial-batches/ ...
   [S3-AWS] Batch flushed -> s3://911transedu-serial-batches/logs/serial_batch_<ts>.txt (...)
   ```

   If a sink fails to arm, the event log says why and the bridge keeps
   running on the local stubs — that is the designed failure mode, not
   an outage.

## Stage 4 — verification

* **Status payload** (public):
  `curl -s http://52.41.84.116:443/usb/api/status` twice, ~15 s apart:
  `sinks.iot_sink` = `AwsIoTClient`, `sinks.mqtt_connected` = true,
  `sinks.mqtt_published` advancing; `sinks.s3_sink` =
  `AwsS3BatchLogger`, `sinks.s3_flushes` ≥ 1, `sinks.s3_last_uri`
  under `s3://911transedu-serial-batches/logs/...`;
  `mqtt_publish_errors` / `s3_flush_errors` staying at 0.
* **DynamoDB** (CloudShell):
  `aws dynamodb scan --table-name serial_events --region us-west-2 --limit 5`
  → items with `pk` like `CELL#1794...`, full v2 envelope attributes.
* **S3** (CloudShell):
  `aws s3 ls s3://911transedu-serial-batches/logs/ --region us-west-2`
  → `serial_batch_<ts>.txt` objects appearing every ~30 s.

## Security notes

* The device private key is generated on the host by
  `bootstrap-certs.sh` and **never leaves it**; only the CSR (public)
  goes to AWS. The secrets dir is bind-mounted **read-only** into the
  usb container. Nothing credential-shaped is in git: the committed
  compose defaults every gate to 0 and takes values from the host
  `.env`.
* The host role's steady-state permission is a single `s3:PutObject`
  on one bucket. The `BootstrapIoT` inline policy (CreateCertificate
  FromCsr / DescribeEndpoint / Attach*) exists only for the one-time
  bootstrap — **remove it after activation verifies**: edit
  `template.yaml` (delete the `BootstrapIoT` policy from `HostRole`),
  re-run the Stage 2 `cloudformation deploy` command, and the role
  drops to S3-only.
* If the bootstrap dies after `CreateCertificateFromCsr` but before
  writing `cert-record.json`, an ACTIVE certificate exists that the
  host doesn't know about. Clean it up in the IoT console
  (Certificates → deactivate → delete) and re-run the bootstrap.
* The MQTT plane is publish-only QoS 1 with mutual TLS 1.2+; the IoT
  policy scopes the certificate to the single client ID and topic.

## Rollback

* **Sinks off, bridge untouched**: set `USB_SOV_AWS_ENABLED=0` in the
  host `.env` and redeploy. The bridge returns to the local stubs;
  AWS resources sit idle (pennies/month at most).
* **Full decommission**:
  ```bash
  aws s3 rm s3://911transedu-serial-batches --recursive --region us-west-2
  aws ec2 modify-instance-attribute --instance-id i-02d813192f607a4ea \
    --instance-profile '{}' --region us-west-2   # detach profile
  aws cloudformation delete-stack --stack-name usb-sovereign-sinks --region us-west-2
  ```
  Then delete `/home/deploy/live-servers/secrets/usb-sovereign/` on
  the host and remove the `USB_SOV_*` lines from the host `.env`.
  (The IoT certificate survives stack deletion only if the stack
  created it — it didn't; deactivate + delete it in the IoT console
  for a complete teardown.)

## Local verification (Stage 1, already done)

`python3 backend/tests_stage1.py` — 25 checks: SigV4 against AWS's
published known-answer vector (both signer copies), IMDS parsing,
MQTT wire encoding + fake-broker round-trip + PUBACK timeout, the full
activation-gate matrix, S3 flush signing/failure retention, and a
behavior smoke subset of the earlier increments (envelope integrity,
signature classification incl. LH/FSH prefixes, sim ingest + COM3
mirror byte purity, failover gate, socat PTY plant). Template linted
with cfn-lint (clean) plus structural checks. No test makes a real
AWS call.
