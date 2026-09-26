# Setup — interview_helper_mux (v2)

Full fresh-env + RSS steps: **[README.md](README.md)**. Agent read order: **[AGENTS.md](AGENTS.md)**.

On **Windows / Linux + NVIDIA** run `./scripts/bootstrap_venv_windows.sh` instead of `bootstrap_venv.sh` (MLX is Apple-only): **[docs/cross-cutting/windows-cuda-setup.md](docs/cross-cutting/windows-cuda-setup.md)**.

```bash
./scripts/bootstrap_venv.sh
mkdir -p config/secrets ASSETS/input
cp config/templates/secrets.env.example config/secrets/secrets.env   # OPENAI_API_KEY
./scripts/run.sh   # http://127.0.0.1:8765
```

**Podcast RSS (Terraform + boto3)** — bucket/layout in `config/app.defaults.json` (`podcast`); AWS creds + CF/feed in `secrets.env`. Infra via `scripts/tf-*.sh` (updates `terraform/state/`). See **[terraform/README.md](terraform/README.md)**. App publish uses boto3 — **no AWS CLI / `aws login`**:

```bash
# AWS_* + OPENAI in config/secrets/secrets.env; confirm podcast.s3_bucket in app.defaults
cp config/terraform.tfvars.example config/terraform.tfvars   # keep s3_bucket_name in sync
./scripts/tf-init.sh && ./scripts/tf-plan.sh && ./scripts/tf-apply.sh
python scripts/seed_podcast_origin.py
./scripts/run.sh
# Seed prints the Apple Podcasts Connect pass-through next to the feed URL.
# Optional recovery: ./scripts/invalidate_podcast_cf.sh
# Session restore: ./scripts/tf-plan.sh --use-session
```

**North star:** [NORTH_STAR.md](NORTH_STAR.md) · RSS: [docs/cross-cutting/podcast-rss-hosting.md](docs/cross-cutting/podcast-rss-hosting.md)

Optional bootstrap flags: `BOOTSTRAP_SKIP_GUI=1`, `BOOTSTRAP_SKIP_VERIFY=1`

Optional run flags: `MUX_PRESERVE_SESSION=1`, `MUX_REBUILD_GUI=1`, `MUX_REFRESH_DEPS=1`, `MUX_SKIP_ASSETS_CLEANUP=1`, `MUX_NO_BROWSER=1`, `MUX_RUN_MODE=manual|full-auto`, `MUX_INPUT_AUDIO=…`, `MUX_FULL_AUTO=1` (Full-auto soft automation; legacy `MUX_BABA_E2E=1` still accepted), `MUX_KEEPALIVE=1` (opt-in Full-auto crash watchdog). Prefer the GUI Start-page Manual/Full-auto control when the browser is open.

Operator journey: [docs/workflows/operator-journey.md](docs/workflows/operator-journey.md)
