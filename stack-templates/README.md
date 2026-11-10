# Stack templates (added 2026-11-10 at Taylor's direction)

Reference pipeline pieces Taylor supplied on 2026-11-10, kept in this repo so they live in the account alongside the working code:

- `ci-cd-ghcr-aws.yml` — GitHub Actions template: build-and-push a Docker image to GHCR (Node 24, Buildx, GHA cache), then a deploy-aws job using an OIDC role (`AWS_ROLE_TO_ASSUME` secret). Its deploy step is a placeholder (echo + comment) until a target service command is chosen.
- `Dockerfile.node24` — two-stage Node 24 Dockerfile (builder + production runner, non-root user, port 3000, `node dist/index.js`).
- `deploy-commands.md` — the matching AWS CLI / Docker / S3-sync command snippets.

## Why these are templates here, not live workflows

This repo and Taylor's other repos already have working pipelines:
- live-servers deploys via `.github/workflows/deploy.yml` (SSH to the AWS host).
- audited-backend / audited-frontend publish to GHCR and deploy by SSH from their own workflows (production run #7 green, 2026-11-08).
- connectable-pathways deploys through Session Manager with its own OIDC role.

Dropping this workflow into `.github/workflows/` of an existing repo as-is would make it run on every push: its build job expects a Node app with a root Dockerfile (the backends are Python/FastAPI; the frontend has its own image build), and its deploy job would fail without the `AWS_ROLE_TO_ASSUME` secret and a real deploy command. To activate it for a specific app, say which app it should own and the deploy target (ECS service, S3+CloudFront, or the EC2 host), and it can be adapted and moved into that repo's workflows.
