# LiqHawk — Deployment

This deployment uses GitHub Actions to build a Docker image, push it to GHCR,
then SSH into a VPS and roll a `docker compose` stack.

## Files

| File | Purpose |
| --- | --- |
| `Dockerfile` | Multi-stage build of the FastAPI backend (Python 3.12 + uv) |
| `.dockerignore` | Keeps secrets, the frontend, and the venv out of the image |
| `docker-compose.yml` | Single-service stack for the VPS — backend only; Mongo lives elsewhere (Atlas / managed cluster) and is reached via `MONGO_URI` in `.env` |
| `.github/workflows/deploy.yml` | Build → push to GHCR → SSH deploy |

## One-time VPS setup

On the server (example: `/opt/liqhawk`):

```bash
# 1. Install Docker + compose plugin (any distro flavor)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out + back in

# 2. Drop the compose file + env in place
sudo mkdir -p /opt/liqhawk && sudo chown "$USER":"$USER" /opt/liqhawk
cd /opt/liqhawk
# Copy docker-compose.yml from the repo to /opt/liqhawk/docker-compose.yml
# Copy .env (filled in with real secrets) to /opt/liqhawk/.env
#   — MONGO_URI MUST point at your live cluster, e.g.:
#     MONGO_URI=mongodb+srv://user:pass@cluster.xxxx.mongodb.net/?retryWrites=true
#   Make sure the VPS's egress IP is allowlisted on the cluster.

# 3. First boot (the workflow will roll subsequent deploys)
echo "GHCR_OWNER=<your-github-username-or-org>" > .env.deploy
echo "LIQHAWK_TAG=latest" >> .env.deploy
docker compose --env-file .env.deploy --env-file .env up -d
```

## GitHub repo secrets

Set these under **Settings → Secrets and variables → Actions**:

| Secret | What it is |
| --- | --- |
| `VPS_HOST` | DNS name or IP of the server |
| `VPS_USER` | SSH user (the one in the docker group) |
| `VPS_SSH_KEY` | Private SSH key, full PEM contents |
| `VPS_PORT` | (optional) SSH port if not 22 |
| `VPS_APP_DIR` | Absolute path on the server, e.g. `/opt/liqhawk` |
| `GHCR_PULL_TOKEN` | A classic PAT with `read:packages` — only needed if your GHCR package is private |

The workflow uses the built-in `GITHUB_TOKEN` to push to GHCR — no extra secret required for the push side.

## What happens on `git push origin main`

1. Checkout + Buildx
2. Log in to GHCR using `GITHUB_TOKEN`
3. Build the image with cache from previous runs
4. Tag and push both `latest` and `sha-<commit>`
5. SSH to the VPS:
   - Write `.env.deploy` with the owner + sha tag
   - `docker compose pull && up -d`
   - Prune dangling images

The `sha-<commit>` tag in `.env.deploy` makes rollbacks easy — set
`LIQHAWK_TAG=sha-abc1234` in `/opt/liqhawk/.env.deploy` and `docker compose up
-d` to roll back.

## Local Docker test

```bash
docker build -t liqhawk:dev .
docker run --rm -p 8000:8000 --env-file .env liqhawk:dev
# In another terminal:
curl http://localhost:8000/health
```

Your local `.env` must point `MONGO_URI` at a Mongo the container can actually
reach — usually your remote cluster (`mongodb+srv://…`). If you also run Mongo
locally on the host, use `mongodb://host.docker.internal:27017` (Linux: add
`--add-host=host.docker.internal:host-gateway` to the `docker run`).
