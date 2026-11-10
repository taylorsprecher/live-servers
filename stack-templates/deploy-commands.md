# Verify AWS profile and active region
aws configure get region --profile default

# Build and test container locally on Node 24
docker build -t app:local .
docker run -p 3000:3000 app:local

# Manual deployment sync (example: frontend static build to S3)
aws s3 sync ./dist s3://your-app-bucket-name \
  --profile default \
  --region us-west-2 \
  --delete
