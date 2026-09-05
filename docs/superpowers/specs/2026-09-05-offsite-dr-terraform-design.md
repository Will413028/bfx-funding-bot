# Offsite DR Terraform and VM Bootstrap Design

**Status:** Approved for implementation
**Date:** 2026-09-05
**Supersedes:** The manual provisioning portion of `2026-09-04-offsite-dr-cloudflare-r2-design.md`

## Goal

Make the Cloudflare R2 and OCI VM preparation repeatable without moving
production credentials into Git, Terraform state, plan artifacts, Docker image
layers, or persistent logs. Terraform owns stable R2 infrastructure; the
existing pgBackRest deployment and systemd files remain the owners of runtime
behavior.

## Decision

Use a hybrid boundary:

| Resource | Owner | Secret handling |
|---|---|---|
| R2 backup bucket | `infra/terraform/r2` | Cloudflare management token is supplied through the process environment |
| R2 lifecycle | `infra/terraform/r2` | Only conservative incomplete-upload cleanup is declared initially |
| R2 runtime token | Operator through the approved Cloudflare R2 flow | Access Key ID and Secret Access Key are captured once and never enter Terraform |
| VM pgBackRest secret fragment | `scripts/setup-pgbackrest-r2.sh` + existing validator | Values are written directly to the VM-only `conf.d/r2.conf` |
| pgBackRest services/timers | Tracked `deploy/vm/systemd/` units + installer | Installation is repeatable; activation remains after real-R2 and restore gates |

One resource has one owner. Terraform must not manage the same timer state,
secret file contents, or pgBackRest retention objects that the deploy/runbook
workflow manages.

## Terraform module

Create `infra/terraform/r2/` as a root module with:

- pinned Terraform and Cloudflare provider versions;
- `cloudflare_r2_bucket` for a private Standard backup bucket;
- `cloudflare_r2_bucket_lifecycle` with only an enabled 24-hour abort rule for
  incomplete multipart uploads;
- outputs for bucket name, S3 endpoint, `auto` region, `path` URI style, and
  `/pgbackrest` prefix;
- no `cloudflare_api_token` resource, secret variables, secret outputs,
  `local_file`, `remote-exec`, or provisioner that writes VM credentials;
- a `.tfvars.example` containing only non-secret values;
- a README describing Cloudflare management authentication, runtime R2 token
  creation, separate Terraform state storage, and plan/apply gates.

The module does not declare object deletion by age. pgBackRest owns repository
retention because an R2 age rule can delete WAL or backup objects still needed
by a valid backup set. Terraform state must use a separate remote backend or
state bucket; it must never share the pgBackRest bucket because the runtime
token is intentionally bucket-scoped.

The provider management credential is read from `CLOUDFLARE_API_TOKEN`; the
runtime credential is the R2 S3 Access Key ID/Secret Access Key pair. These are
different credential classes and must not be substituted for one another.

## VM bootstrap

Create `scripts/setup-pgbackrest-r2.sh` from the repository wizard template.
It runs on the VM from the repository root and has five stages:

1. capture the non-secret Cloudflare account ID and bucket name, derive the
   default-jurisdiction S3 endpoint, and prepare the protected fragment;
2. guide the operator to the R2 API-token page and capture a bucket-scoped
   Object Read & Write Access Key ID and one-time Secret Access Key;
3. capture the pgBackRest repository cipher passphrase through hidden input;
4. run the tracked shared secret validator without printing values;
5. install, but do not enable, the four tracked pgBackRest systemd units.

The wizard uses the existing validator as the final authority. It writes only
the exact five approved options under one `[global]` section, sets directory
mode `0750` and file mode `0640`, and never writes any value to a repository
`.env` file or GitHub secret. Re-running it updates the same VM-only fragment
without printing existing secret values. Timer activation remains an explicit
post-acceptance action in the offsite DR runbook.

Create `scripts/install-pgbackrest-timers.sh` as a non-interactive, idempotent
installer. It verifies Linux/systemd availability, copies the four tracked
units with mode `0644`, runs `daemon-reload`, and leaves both timers disabled
and stopped. After reload it must verify that both timers are inactive and
disabled; if a pre-existing timer is active or enabled, it fails without
mutating that state. It must not call `enable`, `start`, `enable --now`, Docker,
or pgBackRest. The runbook remains the only place that activates timers after
all R2 smoke, disposable expire, isolated restore, cleanup, and fresh-evidence
gates pass.

## Data and control flow

```text
Terraform management token (process env)
        |
        v
  R2 bucket + safe lifecycle
        |
operator creates bucket-scoped R2 S3 token (one-time secret display)
        |
        v
VM wizard -> conf.d/r2.conf -> shared validator
        |
        v
tracked pgBackRest config + postgres container
        |
        v
tracked systemd units -> installer (definitions only)
        |
        v
real-R2 smoke + backup + isolated restore + fresh evidence
        |
        v
operator enables timers in the runbook
```

## Failure and security invariants

- Terraform `plan` and state contain no R2 runtime secret. `sensitive` alone is
  not considered sufficient protection for a secret-bearing resource.
- Terraform plan/apply is never run against the production R2 bucket by the
  coding agent. Production apply and token creation are operator actions.
- Token rotation is create-new, inject, validate, smoke, restore, then revoke;
  Terraform must not automatically destroy the old runtime token.
- The wizard and installer fail closed on missing tools, invalid input, unsafe
  paths, failed ownership/mode changes, or validator failure.
- Timer installation cannot be mistaken for backup acceptance; activation is
  ordered after the existing runbook's measured gates.
- Existing production deploy ownership and application event/projection code
  are unchanged.

## Testing strategy

Add pytest contract tests under
`backend_py/tests/scripts/test_offsite_dr_infra.py` to verify:

- the Terraform module declares only the intended bucket/lifecycle resources;
- Standard storage, safe multipart cleanup, S3 endpoint/region/path outputs,
  and `/pgbackrest` prefix are present;
- no runtime token resource, secret-bearing variable/output, `local_file`,
  `remote-exec`, or shared state/backup bucket instruction is introduced;
- the wizard uses hidden input for secrets, writes the exact fragment, invokes
  the shared validator, and installs without enabling timers;
- the installer copies all four units, reloads systemd, and does not activate
  either timer;
- the runbook orders Terraform/bootstrap and installation before real-R2 and
  restore acceptance, while keeping activation after those gates.

Also run `terraform fmt -check`, `terraform init -backend=false`,
`terraform validate`, `bash -n`, and the repository's complete offline pytest,
mypy, and ruff gates. No test requires R2 credentials, Docker, systemd, or the
production VM.

## Non-goals

- No Terraform management of the VM provider, PostgreSQL data, Docker volumes,
  application env files, R2 runtime tokens, or pgBackRest object retention.
- No production `terraform apply`, R2 token creation, secret injection, timer
  activation, backup, restore, or Bitfinex request in this implementation task.
- No change to the already implemented pgBackRest archive, restore, evidence,
  or Halt 2 state-machine contracts.
