# interview_helper_mux

Turn a long-form interview recording into a **mastered podcast** (`master/master.wav`), with an optional **The War Room** RSS publish path (private S3 + CloudFront).

Details: [NORTH_STAR.md](NORTH_STAR.md) · [SETUP.md](SETUP.md) · [AGENTS.md](AGENTS.md) · [docs/cross-cutting/podcast-rss-hosting.md](docs/cross-cutting/podcast-rss-hosting.md) · [terraform/README.md](terraform/README.md)

Pipeline: **67 stages** (34 analysis + 33 delivery) in [`src/interview_mux/v2/config.py`](src/interview_mux/v2/config.py).

---

## Fresh environment (once per machine)

Requires: macOS Apple Silicon recommended (local STT / image), Python 3.12, `ffmpeg`, Node (for GUI), Terraform `~> 1.14.7` if you will publish RSS.

**Windows / Linux + NVIDIA:** MLX is Apple-only. Use `./scripts/bootstrap_venv_windows.sh`, which builds the CUDA equivalents (faster-whisper, transformers+bitsandbytes, DeepFilterNet wheel) behind the same pipeline contracts — see [docs/cross-cutting/windows-cuda-setup.md](docs/cross-cutting/windows-cuda-setup.md).

```bash
# 1) Clone / enter repo
cd interview_helper_mux

# 2) Bootstrap core .venv + local MLX stacks (speech, LLM, image) + GUI
./scripts/bootstrap_venv.sh

# 3) Secrets
mkdir -p config/secrets ASSETS/input
cp config/templates/secrets.env.example config/secrets/secrets.env
# Edit config/secrets/secrets.env — set at least OPENAI_API_KEY

# 4) Put source interview audio in ASSETS/input/
#    e.g. ASSETS/input/interview.wav
```

Optional bootstrap flags: `BOOTSTRAP_SKIP_GUI=1`, `BOOTSTRAP_SKIP_VERIFY=1`

Episode covers use OpenAI Images (`podcast.cover_image`) — see [docs/cross-cutting/podcast-cover-theme.md](docs/cross-cutting/podcast-cover-theme.md).

---

## Daily launch

```bash
./scripts/run.sh
# open http://127.0.0.1:8765
```

| Step | Command |
|------|---------|
| Setup (once) | `./scripts/bootstrap_venv.sh` |
| Launch GUI | `./scripts/run.sh` |
| Headless | `./scripts/run.sh --cli` |
| Rebuild GUI only | `MUX_REBUILD_GUI=1 ./scripts/run.sh` |
| Detached Full-auto companion | `MUX_FULL_AUTO=1 ./scripts/run.sh` or GUI Start Full-auto (see [smoke-test](docs/workflows/smoke-test.md)) |

Runs and artifacts: `ASSETS/executions/exec_*`

Other run flags: `MUX_PRESERVE_SESSION=1`, `MUX_REFRESH_DEPS=1`, `MUX_SKIP_ASSETS_CLEANUP=1`, `MUX_NO_BROWSER=1`, `MUX_RUN_ID=…`, `MUX_FRESH=0`

---

## The War Room RSS (optional — Terraform)

Infra lives under [`terraform/`](terraform/) with **committed local state** ([terraform/README.md](terraform/README.md)). Bucket name and show/layout settings live in `config/app.defaults.json` → `podcast`. Credentials + CloudFront/feed URLs live in `config/secrets/secrets.env`. Default resource base: **`the_war_room_001`**.

### A — Create podcast hosting (S3 + CloudFront OAC)

```bash
# secrets.env must include AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or AWS_PROFILE)
cp config/terraform.tfvars.example config/terraform.tfvars   # optional overrides
./scripts/tf-init.sh
./scripts/tf-plan.sh
./scripts/tf-apply.sh   # also upserts PODCAST_* into secrets.env
```

Session restore (if needed): `./scripts/tf-plan.sh --use-session` or `USE_LATEST_SESSION=1 ./scripts/tf-plan.sh`.

### B — Seed empty feed + show artwork

```bash
python scripts/seed_podcast_origin.py
```

### C — Run app and publish

```bash
./scripts/run.sh
```

Finish a pipeline → **G-Publish → Prepare package for this run** (local meta/cover/mp3 only). When ready, **Upload this run to S3** (or `python scripts/sync_podcast_episodes.py --execution-id …`). Sync uploads only that execution, is additive, and never deletes remote objects.

Manual CloudFront recovery (boto3, no AWS CLI): `./scripts/invalidate_podcast_cf.sh` (uses the live `PODCAST_FEED_BASE_URL` after apply or `./scripts/tf-rotate-cloudfront-url.sh`).

### D — Submit the feed

Open `PODCAST_FEED_BASE_URL/feed.xml` and submit via Apple’s pass-through:

`https://podcastsconnect.apple.com/my-podcasts/new-feed?submitfeed=<that feed URL>`

Seed / rotate / apply scripts print the encoded pass-through whenever they display a new RSS URL. **Notice:** Apple rejects an empty seed feed — publish ≥1 episode or a trailer first. Also add the same feed URL in Spotify for Podcasters.

Full ops: [docs/cross-cutting/podcast-rss-hosting.md](docs/cross-cutting/podcast-rss-hosting.md)

AWS note: **Terraform** (`scripts/tf-*.sh`) owns the stack and updates `terraform/state/`. App uploads use **boto3** + `secrets.env`. Operators never need `aws login` or AWS CLI.

---

## Layout

```text
scripts/bootstrap_venv.sh          # fresh env
scripts/tf-*.sh                    # Terraform wrappers (podcast stack; updates terraform/state/)
scripts/sync_podcast_tf_secrets.sh # outputs → PODCAST_* in secrets.env
scripts/seed_podcast_origin.py     # seed feed.xml + show art (boto3)
scripts/sync_podcast_episodes.py   # upload one execution (or --all for explicit bulk)
scripts/tf-rotate-cloudfront-url.sh # same S3 bucket, new CloudFront RSS URL
scripts/invalidate_podcast_cf.sh   # CloudFront invalidation of the live feed URL
scripts/run.sh                     # launch (MUX_FULL_AUTO=1 for detached Full-auto)
terraform/                         # S3 + CloudFront OAC (README + committed state/)
src/interview_mux/                 # pipeline + API
frontend/                          # React GUI
config/                            # defaults + secrets + podcast cover + tfvars.example
docs/                              # specs and prompts
tools/                             # CLI helpers + Full-auto launchers
```
