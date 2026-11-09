#!/bin/bash
# LocalStack ready-hook: provision the xsave-dev bucket the way
# template.yaml does in AWS — versioning on, CORS for browser clients.
set -e
awslocal s3api create-bucket --bucket xsave-dev
awslocal s3api put-bucket-versioning \
  --bucket xsave-dev \
  --versioning-configuration Status=Enabled
awslocal s3api put-bucket-cors \
  --bucket xsave-dev \
  --cors-configuration '{
    "CORSRules": [{
      "AllowedOrigins": ["*"],
      "AllowedMethods": ["GET", "PUT", "DELETE", "HEAD"],
      "AllowedHeaders": ["*"],
      "ExposeHeaders": ["ETag"],
      "MaxAgeSeconds": 3000
    }]
  }'
echo "xsave-dev bucket ready"
