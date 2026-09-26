# Config

Resolution order (highest wins):

1. CLI flags (`--input`, `--run-id`)
2. `config/secrets/secrets.env`
3. `config/app.local.json` (gitignored, per-machine)
4. `config/app.defaults.json`

## Files

| File | Committed | Purpose |
|------|-----------|---------|
| `app.defaults.json` | yes | Paths, model routing, `podcast.*` (bucket/layout/show meta) |
| `templates/app.defaults.json` | yes | Copy template |
| `templates/secrets.env.example` | yes | Secrets template |
| `app.local.json` | **gitignored** | Per-machine overrides deep-merged over `app.defaults.json` (local runtime venv paths, model tiers) |
| `secrets/secrets.env` | **gitignored** | API keys + AWS creds + derived `PODCAST_*` |
| `terraform.tfvars.example` | yes | Copy → `terraform.tfvars` for Terraform (`scripts/tf-*.sh`) |
| `terraform.tfvars` | **gitignored** | Operator Terraform variable values |
| `podcast/` | yes | Show art + cover theme |

Key-by-key semantics: [docs/cross-cutting/config-keys.md](../docs/cross-cutting/config-keys.md).  
Podcast AWS: [docs/cross-cutting/podcast-rss-hosting.md](../docs/cross-cutting/podcast-rss-hosting.md) · [terraform/README.md](../terraform/README.md) — **Terraform** updates `terraform/state/`; app uses **boto3** (never AWS CLI / `aws login`).

Secrets are loaded by Python into an isolated dict — not exported to `os.environ` globally (Terraform wrappers source `secrets.env` for provider auth only).
