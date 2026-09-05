# Secret-free Cloudflare R2 module

This root module creates the private Standard R2 bucket used by pgBackRest and
one conservative lifecycle rule: abort incomplete multipart uploads after 24
hours. pgBackRest, not R2 lifecycle, owns backup retention; object-age deletion
could remove WAL or backup objects needed by a valid backup set.

## Authentication and state boundary

Terraform authenticates Cloudflare management requests with
`CLOUDFLARE_API_TOKEN` from the operator's process environment. It is distinct
from the bucket-scoped R2 S3 Access Key ID and Secret Access Key used at runtime
by pgBackRest. Runtime R2 credentials are created through the approved operator
flow, captured once, and injected only into the VM-only secret fragment; they
must never enter Terraform variables, state, plans, or Git.

The Terraform state bucket must never be the same bucket as
`bfx-funding-bot-pgbackrest`. Provision the independent state bucket
`bfx-funding-bot-terraform-state` and its state-bucket runtime token separately
before `terraform init`; keep its credentials outside Git.

## Operator workflow

Copy both examples outside Git and replace only their non-secret placeholders.
Run these commands from this directory:

```bash
cp backend.hcl.example /secure/path/backend.hcl
cp terraform.tfvars.example /secure/path/terraform.tfvars
read -r -s -p 'Cloudflare management token (hidden; not saved to history): ' CLOUDFLARE_API_TOKEN
printf '\n'
export CLOUDFLARE_API_TOKEN
terraform init -backend-config=/secure/path/backend.hcl
terraform plan -var-file=/secure/path/terraform.tfvars -out=/secure/path/r2.tfplan
terraform apply /secure/path/r2.tfplan
```

For an already existing bucket, import it before planning. Use the bucket's
actual jurisdiction in place of `<jurisdiction>`:

```bash
terraform import -var-file=/secure/path/terraform.tfvars \
  cloudflare_r2_bucket.backup '<account_id>/<bucket_name>/<jurisdiction>'
```

The lifecycle resource does not support import; after importing an existing
bucket, review the first plan carefully before applying lifecycle changes.

Production apply, creation of the management token and runtime R2 credentials,
secret injection, and every restore acceptance gate are operator-only actions.
Do not apply this module, enable backup timers, or treat a backup as accepted
until the documented R2 smoke, disposable-expire, isolated-restore, cleanup,
and fresh-evidence gates have passed.
