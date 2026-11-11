#!/usr/bin/env python3
"""Provision the REAL Xbox Cloud Save backend from Taylor's
"Xbox AWS S3 1" design, in Taylor's AWS **member** account.

    Account:  589997396938 ("Index Zero")  — HARD GUARD below
    Region:   us-west-2
    Creates:  S3 bucket (SSE encryption, public access blocked,
              versioning on) · least-privilege IAM role (only
              s3:GetObject / s3:PutObject on this bucket's users/*
              prefix, plus CloudWatch Logs for the function) · Python
              Lambda (lambda_presign.py: POST /saves/{saveId}/upload-url,
              GET /saves/{saveId}/download-url, 15-minute pre-signed
              URLs, keys users/{xuid}/savefiles/{saveId}) · API Gateway
              HTTP API with those two routes · CloudWatch logging
              (Lambda log group + API access logs). Free-tier scale:
              no provisioned concurrency, no paid extras.

Run it ONLY from an authenticated AWS session for the member account
(e.g. Taylor's AWS CloudShell after signing in to "Index Zero").
There are deliberately NO credentials in this repo: boto3 uses the
ambient session, and the first thing this script does — before any
mutating call — is sts:GetCallerIdentity and an abort unless the
account is exactly 589997396938. (The organization management account
392349106458 is a documented trap: the console defaults to it.)

    python3 provision_aws.py                      # create / converge
    python3 provision_aws.py --enable-test-identity
        # also sets the clearly-labeled XSAVE_TEST_XUID on the Lambda
        # so verify_aws.py can prove the chain before the Microsoft
        # Xbox Live relying-party registration exists. Remove it for
        # any real use (see aws/README.md).
    python3 provision_aws.py --destroy            # tear it all down

Idempotent: every resource is create-or-reuse / create-or-update.
Requires boto3 (present in AWS CloudShell and any standard AWS dev
environment).
"""

import argparse
import io
import json
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

EXPECTED_ACCOUNT = "589997396938"
DEFAULT_REGION = "us-west-2"
DEFAULT_BUCKET = f"xbox-cloud-saves-{EXPECTED_ACCOUNT}-us-west-2"
FUNCTION_NAME = "xsave-presign-api"
ROLE_NAME = "xsave-lambda-exec"
API_NAME = "xsave-api"
API_LOG_GROUP = "/aws/apigateway/xsave-api"
FUNCTION_LOG_GROUP = f"/aws/lambda/{FUNCTION_NAME}"
DEFAULT_TEST_XUID = "9000000000000001"  # clearly-labeled test identity

ROUTES = ["POST /saves/{saveId}/upload-url", "GET /saves/{saveId}/download-url"]


def die(msg):
    print(f"ABORT: {msg}")
    sys.exit(1)


def note(msg):
    print(f"  {msg}")


# ------------------------------------------------------------------ guard

def assert_account(sts):
    ident = sts.get_caller_identity()
    account = ident["Account"]
    print(f"caller: {ident['Arn']}")
    print(f"account: {account}")
    if account != EXPECTED_ACCOUNT:
        die(f"wrong account — expected member account {EXPECTED_ACCOUNT} "
            f"('Index Zero'), got {account}. Nothing was created. "
            "Sign in to the member account and re-run.")
    return account


# ------------------------------------------------------------------ s3

def ensure_bucket(s3, bucket, region):
    try:
        s3.head_bucket(Bucket=bucket)
        note(f"bucket exists: {bucket}")
    except ClientError:
        kwargs = {"Bucket": bucket}
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        note(f"bucket created: {bucket}")
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={"Rules": [{
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
            "BucketKeyEnabled": True,
        }]},
    )
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    note("bucket hardened: public access blocked, SSE default "
         "encryption on, versioning on")


# ------------------------------------------------------------------ iam

def ensure_role(iam, bucket, account, region):
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        note(f"role exists: {ROLE_NAME}")
    except ClientError:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Xbox Cloud Save presign Lambda — least privilege",
        )["Role"]
        note(f"role created: {ROLE_NAME}")
    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "SaveObjectsOnlyUnderUsersPrefix",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": f"arn:aws:s3:::{bucket}/users/*",
        }],
    }
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="xsave-s3-saves",
                        PolicyDocument=json.dumps(s3_policy))
    logs_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "FunctionLogsOnly",
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                       "logs:PutLogEvents"],
            "Resource": [
                f"arn:aws:logs:{region}:{account}:log-group:{FUNCTION_LOG_GROUP}",
                f"arn:aws:logs:{region}:{account}:log-group:{FUNCTION_LOG_GROUP}:*",
            ],
        }],
    }
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="xsave-logs",
                        PolicyDocument=json.dumps(logs_policy))
    note("role policies converged: s3:GetObject/PutObject on "
         f"arn:aws:s3:::{bucket}/users/* ONLY, + function logs")
    return role["Arn"]


# ------------------------------------------------------------------ lambda

def build_zip():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "lambda_presign.py"), "rb") as fh:
        code = fh.read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", code)
    return buf.getvalue()


def ensure_function(lam, role_arn, bucket, test_xuid):
    zip_bytes = build_zip()
    env = {"Variables": {"XSAVE_BUCKET": bucket, "XSAVE_TEST_XUID": test_xuid or ""}}
    try:
        cfg = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=zip_bytes)
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME, Role=role_arn,
            Handler="lambda_function.handler", Runtime="python3.12",
            Timeout=15, MemorySize=256, Environment=env)
        note(f"function updated: {FUNCTION_NAME}")
        arn = cfg["FunctionArn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        arn = lam.create_function(
            FunctionName=FUNCTION_NAME, Runtime="python3.12", Role=role_arn,
            Handler="lambda_function.handler",
            Code={"ZipFile": zip_bytes}, Timeout=15, MemorySize=256,
            Environment=env,
            Description="Xbox Cloud Save presigned-URL API (Xbox AWS S3 1 design)",
        )["FunctionArn"]
        note(f"function created: {FUNCTION_NAME}")
    for _ in range(30):
        state = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"].get("State")
        if state == "Active":
            break
        time.sleep(2)
    return arn


# ------------------------------------------------------------------ api gw

def ensure_api(apigw, logs, lam, function_arn, account, region):
    api = None
    for page in apigw.get_paginator("get_apis").paginate():
        for item in page.get("Items", []):
            if item["Name"] == API_NAME:
                api = item
                break
    if api is None:
        api = apigw.create_api(Name=API_NAME, ProtocolType="HTTP",
                               Description="Xbox Cloud Save save API (Xbox AWS S3 1 design)")
        note(f"api created: {api['ApiId']}")
    else:
        note(f"api exists: {api['ApiId']}")
    api_id = api["ApiId"]

    integration_id = None
    for item in apigw.get_integrations(ApiId=api_id).get("Items", []):
        if (item.get("IntegrationType") == "AWS_PROXY"
                and item.get("IntegrationUri") == function_arn):
            integration_id = item["IntegrationId"]
            break
    if integration_id is None:
        integration = apigw.create_integration(
            ApiId=api_id, IntegrationType="AWS_PROXY",
            IntegrationUri=function_arn, PayloadFormatVersion="2.0")
        integration_id = integration["IntegrationId"]
    existing = {r["RouteKey"] for r in
                apigw.get_routes(ApiId=api_id).get("Items", [])}
    for route in ROUTES:
        if route in existing:
            # Converge the target: delete + recreate the route.
            for r in apigw.get_routes(ApiId=api_id).get("Items", []):
                if r["RouteKey"] == route:
                    apigw.delete_route(ApiId=api_id, RouteId=r["RouteId"])
        apigw.create_route(ApiId=api_id, RouteKey=route,
                           Target=f"integrations/{integration_id}")
    note(f"routes converged: {', '.join(ROUTES)}")

    try:
        logs.create_log_group(logGroupName=API_LOG_GROUP)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    log_arn = f"arn:aws:logs:{region}:{account}:log-group:{API_LOG_GROUP}"
    access = {
        "DestinationArn": log_arn,
        "Format": json.dumps({"requestId": "$context.requestId",
                              "routeKey": "$context.routeKey",
                              "status": "$context.status",
                              "latency": "$context.responseLatency"}),
    }
    stages = apigw.get_stages(ApiId=api_id).get("Items", [])
    if any(s["StageName"] == "$default" for s in stages):
        apigw.update_stage(ApiId=api_id, StageName="$default",
                           AccessLogSettings=access)
    else:
        apigw.create_stage(ApiId=api_id, StageName="$default",
                           AutoDeploy=True, AccessLogSettings=access)
    note("stage $default: auto-deploy, CloudWatch access logging on")

    execution_arn = f"arn:aws:execute-api:{region}:{account}:{api_id}"
    try:
        lam.add_permission(FunctionName=FUNCTION_NAME,
                           StatementId="apigateway-invoke",
                           Action="lambda:InvokeFunction",
                           Principal="apigateway.amazonaws.com",
                           SourceArn=f"{execution_arn}/*/*")
        note("lambda invoke permission granted to API Gateway")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceConflictException":
            raise
        note("lambda invoke permission already present")
    return api["ApiEndpoint"]


# ------------------------------------------------------------------ destroy

def destroy(session, region, bucket):
    sts = session.client("sts")
    account = assert_account(sts)
    apigw = session.client("apigatewayv2", region_name=region)
    lam = session.client("lambda", region_name=region)
    iam = session.client("iam")
    s3 = session.client("s3", region_name=region)
    logs = session.client("logs", region_name=region)
    for page in apigw.get_paginator("get_apis").paginate():
        for item in page.get("Items", []):
            if item["Name"] == API_NAME:
                apigw.delete_api(ApiId=item["ApiId"])
                note(f"api deleted: {item['ApiId']}")
    try:
        lam.delete_function(FunctionName=FUNCTION_NAME)
        note("function deleted")
    except ClientError:
        pass
    for policy in ("xsave-s3-saves", "xsave-logs"):
        try:
            iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName=policy)
        except ClientError:
            pass
    try:
        iam.delete_role(RoleName=ROLE_NAME)
        note("role deleted")
    except ClientError:
        pass
    try:
        paginator = s3.get_paginator("list_object_versions")
        batch = []
        for page in paginator.paginate(Bucket=bucket):
            for v in page.get("Versions", []) + page.get("DeleteMarkers", []):
                batch.append({"Key": v["Key"], "VersionId": v["VersionId"]})
        for i in range(0, len(batch), 1000):
            if batch[i:i + 1000]:
                s3.delete_objects(Bucket=bucket,
                                  Delete={"Objects": batch[i:i + 1000]})
        s3.delete_bucket(Bucket=bucket)
        note(f"bucket emptied + deleted: {bucket}")
    except ClientError:
        note("bucket already gone")
    for group in (API_LOG_GROUP, FUNCTION_LOG_GROUP):
        try:
            logs.delete_log_group(logGroupName=group)
        except ClientError:
            pass
    print(f"DESTROYED in account {account}. Nothing else was touched.")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--enable-test-identity", nargs="?", const=DEFAULT_TEST_XUID,
                    default=None, metavar="XUID",
                    help="set the labeled test identity on the Lambda "
                         "(default XUID 9000000000000001) for verification")
    ap.add_argument("--destroy", action="store_true")
    args = ap.parse_args()

    session = boto3.session.Session(region_name=args.region)
    if args.destroy:
        destroy(session, args.region, args.bucket)
        return

    sts = session.client("sts")
    account = assert_account(sts)
    s3 = session.client("s3", region_name=args.region)
    iam = session.client("iam")
    lam = session.client("lambda", region_name=args.region)
    apigw = session.client("apigatewayv2", region_name=args.region)
    logs = session.client("logs", region_name=args.region)

    print("=== XBOX CLOUD SAVE — REAL AWS PROVISION (member account) ===")
    ensure_bucket(s3, args.bucket, args.region)
    role_arn = ensure_role(iam, args.bucket, account, args.region)
    note("waiting 10s for IAM propagation…")
    time.sleep(10)
    function_arn = ensure_function(lam, role_arn, args.bucket,
                                   args.enable_test_identity)
    endpoint = ensure_api(apigw, logs, lam, function_arn, account, args.region)

    print("\n=== PROVISIONED (real AWS, account "
          f"{account}, {args.region}) ===")
    print(json.dumps({
        "account": account,
        "region": args.region,
        "bucket": args.bucket,
        "role": ROLE_NAME,
        "function": FUNCTION_NAME,
        "apiEndpoint": endpoint,
        "routes": [f"{endpoint}{r.split(' ', 1)[1]}" for r in ROUTES],
        "keyLayout": "users/{xuid}/savefiles/{saveId}",
        "presignExpirySeconds": 900,
        "testIdentityEnabled": bool(args.enable_test_identity),
    }, indent=2))
    if args.enable_test_identity:
        print("\nWARNING: the labeled TEST identity is ENABLED on the "
              "Lambda (XSAVE_TEST_XUID). It exists only to verify the "
              "chain before the Microsoft Xbox Live registration "
              "exists. Remove it for any real use — see aws/README.md.")
    print("\nXSTS status: the Lambda validates tokens against the real "
          "Xbox Live profile service, but real tokens require a "
          "Microsoft relying-party registration Taylor does not yet "
          "have — see aws/README.md.")


if __name__ == "__main__":
    main()
