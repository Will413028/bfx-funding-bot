# Offsite DR Terraform and VM Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secret-safe Terraform R2 module and repeatable VM bootstrap path while keeping pgBackRest runtime ownership in the existing deploy scripts and systemd units.

**Architecture:** Terraform declares only the private Standard R2 backup bucket and a conservative incomplete-multipart-upload lifecycle rule. A wizard creates the bucket-scoped R2 S3 credential and repository cipher fragment directly on the VM, using the existing validator; a separate installer copies tracked systemd definitions but never activates them. The runbook orders operator `apply`, token/secret bootstrap, real-R2 acceptance, isolated restore, and timer activation.

**Tech Stack:** Terraform 1.14+, Cloudflare Terraform provider 5.x, Cloudflare R2 S3 API, Bash, systemd, Python 3.13, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-offsite-dr-terraform-design.md`

## Global Constraints

- R2 uses S3 region `auto`, path URI style, a private Standard bucket, `/pgbackrest` prefix, and a bucket-scoped Object Read & Write runtime token.
- Terraform must not declare a runtime token resource, secret variable/output, `local_file`, `remote-exec`, or provisioner that writes VM credentials.
- Terraform state must use a separate remote backend or state bucket; it must never share the pgBackRest backup bucket.
- R2 lifecycle may abort incomplete multipart uploads after 24 hours, but must not delete objects by age; pgBackRest owns repository retention.
- The VM secret fragment contains exactly `repo1-s3-endpoint`, `repo1-s3-bucket`, `repo1-s3-key`, `repo1-s3-key-secret`, and `repo1-cipher-pass` once each under one `[global]` section.
- VM secret directory/file permissions are `0750`/`0640`, the PostgreSQL container identity is UID/GID 70, and no secret value may enter Git, CI variables, shell history, Docker image layers, logs, or evidence.
- The existing `deploy/vm/pgbackrest/secret_validation.py` is the shared validation authority; the wizard must invoke it without printing values.
- The timer installer must copy the four tracked pgBackRest service/timer units with mode `0644`, run `daemon-reload`, and leave both timers disabled and stopped.
- Timer activation stays after real-R2 smoke, disposable `expire`, isolated restore, cleanup, and fresh measured-evidence gates in `docs/runbooks/offsite-dr.md`.
- No production `terraform apply`, R2 token creation, VM secret injection, backup, restore, timer activation, or Bitfinex request is executed by the coding agent.
- Every change includes pytest coverage; use `cd backend_py && uv run pytest`, never `unittest`.
- Before handoff run `cd backend_py && uv run pytest -m "not integration"`, `uv run mypy src/`, `uv run ruff check`, Terraform formatting/validation, and Bash syntax checks.

---

### Task 1: Add the secret-free Terraform R2 module

**Files:**
- Create: `infra/terraform/r2/versions.tf`
- Create: `infra/terraform/r2/backend.tf`
- Create: `infra/terraform/r2/backend.hcl.example`
- Create: `infra/terraform/r2/variables.tf`
- Create: `infra/terraform/r2/main.tf`
- Create: `infra/terraform/r2/outputs.tf`
- Create: `infra/terraform/r2/terraform.tfvars.example`
- Create: `infra/terraform/r2/README.md`
- Modify: `.gitignore`
- Test: `backend_py/tests/scripts/test_offsite_dr_terraform.py`

**Interfaces:**
- Consumes: `CLOUDFLARE_API_TOKEN` from the operator's process environment and non-secret variables `cloudflare_account_id`, `backup_bucket_name`, and nullable `bucket_location`.
- Produces: Terraform resources `cloudflare_r2_bucket.backup` and `cloudflare_r2_bucket_lifecycle.backup`; outputs `bucket_name`, `s3_endpoint`, `s3_region`, `s3_uri_style`, and `repo_path`.
- Does not produce: R2 runtime Access Key ID, Secret Access Key, VM files, or Terraform-managed systemd state.

- [ ] **Step 1: Write the failing pytest contract tests.**

Create `backend_py/tests/scripts/test_offsite_dr_terraform.py` with these
behaviors:

```python
def test_module_declares_standard_backup_bucket_and_safe_lifecycle() -> None:
    assert 'resource "cloudflare_r2_bucket" "backup"' in main_text
    assert 'storage_class = "Standard"' in main_text
    assert 'resource "cloudflare_r2_bucket_lifecycle" "backup"' in lifecycle_text
    assert 'max_age = 86400' in lifecycle_text
    assert 'abort_multipart_uploads_transition' in lifecycle_text
    assert 'delete_objects_transition' not in lifecycle_text


def test_module_outputs_pgbackrest_s3_contract_without_runtime_secret() -> None:
    assert '"https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"' in outputs_text
    assert 'value       = "auto"' in outputs_text
    assert 'value       = "path"' in outputs_text
    assert 'value       = "/pgbackrest"' in outputs_text
    assert 'secret_access_key' not in terraform_text
    assert 'repo1-s3-key-secret' not in terraform_text


def test_module_does_not_manage_tokens_or_vm_files() -> None:
    forbidden = ("cloudflare_api_token", "local_file", "remote-exec", "provisioner")
    assert not any(item in terraform_text for item in forbidden)


def test_state_example_is_separate_from_backup_bucket() -> None:
    assert 'bfx-funding-bot-terraform-state' in backend_example_text
    assert 'bfx-funding-bot-pgbackrest' in tfvars_example_text
    assert 'same bucket' in readme_text.lower()
```

The test module must derive `ROOT = Path(__file__).resolve().parents[3]`, read
the exact module files, and assert that `.tfvars.example` contains no
`AWS_SECRET_ACCESS_KEY`, `CLOUDFLARE_API_TOKEN`, `repo1-s3-key-secret`, or
`repo1-cipher-pass` assignment. Do not parse HCL with a new dependency.

- [ ] **Step 2: Run the focused test and verify RED.**

Run:

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_terraform.py -q
```

Expected: collection or assertion failures because `infra/terraform/r2/` and
the new contract test do not yet have the module files.

- [ ] **Step 3: Implement the minimum Terraform module.**

Create `versions.tf` with Terraform `>= 1.14.0, < 2.0.0`, Cloudflare provider
source `cloudflare/cloudflare`, and a pinned `~> 5.24` provider constraint.
Configure the provider to use its normal `CLOUDFLARE_API_TOKEN` environment
authentication; do not put a token literal in HCL.

Create `backend.tf` with an empty S3 backend block so real state configuration
is supplied outside Git. Create `backend.hcl.example` using an independent
state bucket named `bfx-funding-bot-terraform-state`, S3 region `auto`, the R2
endpoint shape, path-style requests, and the R2-compatible skip flags. The
example must contain no credentials. Explain that the state bucket and
runtime state token are provisioned separately before `terraform init`.

Declare these variable contracts in `variables.tf`:

```hcl
variable "cloudflare_account_id" {
  type        = string
  description = "32-character Cloudflare account ID."
  validation {
    condition     = can(regex("^[a-f0-9]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be a lowercase 32-character hexadecimal ID."
  }
}

variable "backup_bucket_name" {
  type        = string
  description = "Dedicated private R2 bucket for pgBackRest objects."
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.backup_bucket_name))
    error_message = "backup_bucket_name must be 3-63 lowercase characters, digits, or hyphens."
  }
}

variable "bucket_location" {
  type        = string
  default     = null
  nullable    = true
  description = "Optional first-create R2 location hint; null leaves the provider default."
}
```

Add validation for `bucket_location` when non-null against Cloudflare's
`apac`, `eeur`, `enam`, `weur`, `wnam`, and `oc` location hints. Do not invent
or silently change a jurisdiction.

Create `main.tf` with exactly the backup bucket and lifecycle resources. Set
the bucket `storage_class` to `Standard`, pass the optional location, and set
the lifecycle rule ID to `abort-incomplete-multipart-uploads`, prefix to the
empty string, enabled to true, type to `Age`, and max age to `86400`. Do not
declare age-based object deletion or Object Lock.

Create `outputs.tf` with the five non-secret outputs named above. The endpoint
must be derived as
`https://${var.cloudflare_account_id}.r2.cloudflarestorage.com`; region is
`auto`; URI style is `path`; and repo path is `/pgbackrest`.

Create `terraform.tfvars.example` with a zeroed 32-character example account
ID, `bfx-funding-bot-pgbackrest` as the bucket name, and `bucket_location = null`.
Create the README with exact init/plan/apply commands, the separation between
Cloudflare management and R2 S3 runtime credentials, the separate state bucket
rule, import guidance for an already existing bucket, and the operator-only
production apply/token/secret/restore gates. State explicitly that pgBackRest,
not R2 lifecycle, owns backup retention.

Add `.terraform/`, `*.tfstate`, `*.tfstate.*`, `*.tfplan`, and real
`terraform.tfvars` files to `.gitignore` while keeping
`terraform.tfvars.example` trackable. Do not ignore the provider lock file.

- [ ] **Step 4: Run the focused tests and Terraform validation.**

Run:

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_terraform.py -q
cd ../infra/terraform/r2
terraform fmt -check
terraform init -backend=false
terraform validate
```

Expected: all pytest contracts pass, Terraform formatting reports no changes,
provider initialization succeeds without Cloudflare credentials, and
`terraform validate` exits zero. `terraform init` may create ignored
`.terraform/` data and a trackable provider lock file; inspect the diff before
staging.

- [ ] **Step 5: Commit the Terraform module.**

```bash
git add .gitignore infra/terraform/r2 backend_py/tests/scripts/test_offsite_dr_terraform.py
git commit -m "✨ Feat: add secret-free R2 Terraform module"
```

---

### Task 2: Add VM secret wizard and definition-only timer installer

**Files:**
- Create: `scripts/setup-pgbackrest-r2.sh`
- Create: `scripts/install-pgbackrest-timers.sh`
- Create: `backend_py/tests/scripts/test_offsite_dr_bootstrap.py`

**Interfaces:**
- Consumes: Terraform's non-secret bucket/account output, operator-copied R2 S3 credentials, an operator-entered repository cipher passphrase, and existing `deploy/vm/pgbackrest/secret_validation.py`.
- Produces: VM-only `$HOME/bfx/pgbackrest/conf.d/r2.conf` with the exact five approved options; installed but inactive systemd unit definitions under `/etc/systemd/system`.
- Does not produce: repository `.env` values, GitHub secrets, Terraform state changes, timer activation, Docker commands, or pgBackRest commands.

- [ ] **Step 1: Write failing bootstrap contract tests.**

Create `backend_py/tests/scripts/test_offsite_dr_bootstrap.py` with static
contracts for the generated scripts:

```python
def test_secret_wizard_captures_only_vm_secret_fragment() -> None:
    stage_text = wizard_text.split("# STAGES", 1)[1]
    assert 'ENV_FILE="$SECRET_FILE"' in stage_text
    assert "ask_secret R2_SECRET_ACCESS_KEY" in stage_text
    assert "ask_secret R2_CIPHER_PASS" in stage_text
    assert 'repo1-s3-key-secret' in stage_text
    assert 'repo1-cipher-pass' in stage_text
    assert 'secret_validation.py" --secret-dir' in stage_text
    assert 'install-pgbackrest-timers.sh' in stage_text
    assert 'set_secret ' not in stage_text
    assert 'BFX_API_SECRET' not in stage_text


def test_timer_installer_is_definition_only() -> None:
    for unit_name in EXPECTED_UNITS:
        assert f"{unit_name}" in installer_text
    assert "systemctl daemon-reload" in installer_text
    assert "systemctl enable" not in installer_text
    assert "systemctl start" not in installer_text
    assert "enable --now" not in installer_text
    assert "docker" not in installer_text.lower()
    assert "pgbackrest" not in installer_text.lower()
```

Also assert the wizard source contains the template library marker, the five
exact option names in its stage section, mode `0750`, mode `0640`, and the R2
authentication documentation URL. The installer test must require all four
unit names and the destination `/etc/systemd/system`.

- [ ] **Step 2: Run the focused test and verify RED.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_bootstrap.py -q
```

Expected: import or assertion failures because both scripts are not present.

- [ ] **Step 3: Implement the wizard from the standard wizard template.**

Copy the complete wizard library from the repository's wizard skill template
into `scripts/setup-pgbackrest-r2.sh` and leave the library above the `STAGES`
marker unchanged. Below the marker set `TOTAL_STAGES=5`, derive `ROOT` from
the script location, set `SECRET_DIR` from `PGBACKREST_SECRET_DIR` or
`$HOME/bfx/pgbackrest/conf.d`, set `SECRET_FILE="$SECRET_DIR/r2.conf"`, and
override `ENV_FILE` to that VM-only file.

The five stages must do the following in order:

1. Create the directory and a `[global]` file header when absent, ask for a
   lowercase 32-character Cloudflare account ID and a 3-63 character bucket
   name, derive the default endpoint
   `https://ACCOUNT_ID.r2.cloudflarestorage.com`, and write only the endpoint
   and bucket options.
2. Open `https://developers.cloudflare.com/r2/api/tokens/`, instruct the
   operator to select Object Read & Write and scope it to the Terraform-created
   bucket, then capture Access Key ID visibly and Secret Access Key with
   `ask_secret`; write the two values without printing them.
3. Capture the repository cipher passphrase with `ask_secret` and write it as
   `repo1-cipher-pass`; explain that it needs separate offline escrow.
4. Apply owner/group and `0750`/`0640` permissions, run the existing validator
   with `--secret-dir`, and stop on any nonzero result. A successful validator
   prints no secret values.
5. Run `scripts/install-pgbackrest-timers.sh` to install unit definitions only;
   explicitly say activation is deferred until the runbook acceptance gates.

Use `write_env` for each persisted option, `ask_secret` for the Secret Access
Key and cipher passphrase, and no `set_secret`. The script must never echo a
captured value, set `-x`, write to `.env`, or read application `bot.env`,
`webapi.env`, or `frontend.env`. Re-runs keep existing values when the operator
presses Enter and finish by reapplying safe ownership/modes.

- [ ] **Step 4: Implement the definition-only timer installer.**

Create `scripts/install-pgbackrest-timers.sh` with `set -euo pipefail`. Resolve
the repository root from `BASH_SOURCE`, require `uname` to report Linux and
`systemctl` to exist, then install exactly these four tracked files with mode
`0644` into `/etc/systemd/system`:

```text
bfx-pgbackrest-backup.service
bfx-pgbackrest-backup.timer
bfx-pgbackrest-status.service
bfx-pgbackrest-status.timer
```

Run `sudo systemctl daemon-reload`, print only the destination and a message
that timers remain disabled/stopped, and exit zero. Do not call `enable`,
`start`, `enable --now`, Docker, pgBackRest, or any application command. A
missing source unit, non-Linux host, missing `sudo`, failed install, failed
daemon reload, or a pre-existing active/enabled timer must return nonzero. After
reload, check both timers with `systemctl is-active` and `systemctl is-enabled`;
if either reports active/enabled, fail without stopping or disabling it. Keep
the existing runbook as the gate that activates timers after measured
acceptance.

- [ ] **Step 5: Run focused tests and shell checks.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_bootstrap.py -q
cd ..
bash -n scripts/setup-pgbackrest-r2.sh scripts/install-pgbackrest-timers.sh
shellcheck scripts/setup-pgbackrest-r2.sh scripts/install-pgbackrest-timers.sh
```

Expected: all pytest contracts pass, both scripts parse, and shellcheck emits
no diagnostics. Do not run the wizard end-to-end; it is intentionally
interactive and writes VM credentials.

- [ ] **Step 6: Commit the VM bootstrap scripts.**

```bash
git add scripts/setup-pgbackrest-r2.sh scripts/install-pgbackrest-timers.sh backend_py/tests/scripts/test_offsite_dr_bootstrap.py
git commit -m "✨ Feat: add secret-safe VM DR bootstrap"
```

---

### Task 3: Wire the operator runbook to the single owners

**Files:**
- Modify: `docs/runbooks/offsite-dr.md`
- Modify: `backend_py/tests/scripts/test_offsite_dr_ops.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Terraform module commands from Task 1, VM wizard and timer installer from Task 2, existing pgBackRest acceptance sections, and existing timer activation gate.
- Produces: one ordered operator procedure in which Terraform provisions the bucket, the wizard provisions secrets, the installer installs definitions, and only Section 9 activates timers after measured acceptance.

- [ ] **Step 1: Write failing runbook-order tests.**

Extend the existing runbook contract test so it requires these markers in this
order before the existing real-R2 gates:

```python
(
    "### 0. Provision the R2 backup bucket with Terraform",
    "terraform init -backend-config=backend.hcl",
    "terraform plan -out=r2.tfplan",
    "terraform apply r2.tfplan",
    "./scripts/install-pgbackrest-timers.sh",
    "./scripts/setup-pgbackrest-r2.sh",
    "Build and validate bfx-postgres:local",
    "`stanza-create`",
    "Run the staged isolated restore with --baseline",
    "Accept only fresh measured evidence",
    "Enable, start, and list the timers",
)
```

Change the existing unit-install assertions to require the installer command
instead of duplicating four `sudo install` commands in the runbook. Preserve
the existing assertion that every timer `enable`/`start` command occurs after
the `### 9. Enable, start, and list the timers` heading. Add assertions that
the runbook says the state bucket is separate, the runtime token is not a
Terraform resource, R2 lifecycle does not own pgBackRest retention, and the
agent does not perform production apply/token creation.

- [ ] **Step 2: Run the focused test and verify RED.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_ops.py -q -k 'runbook'
```

Expected: failures for the missing Terraform/bootstrap ordering and the
runbook's old inline unit-install commands.

- [ ] **Step 3: Update the runbook without changing DR acceptance gates.**

Insert `### 0. Provision the R2 backup bucket with Terraform` before the
existing numbered procedure. Document that the operator, not the coding
agent, supplies `CLOUDFLARE_API_TOKEN`, uses an independent state bucket, runs
`terraform init -backend-config=backend.hcl`, reviews `terraform plan
-out=r2.tfplan`, and applies only the reviewed plan. State that an existing
bucket must be imported before planning and that the module does not create a
runtime R2 token.

Replace the four inline unit-copy commands in Section 1 with
`./scripts/install-pgbackrest-timers.sh`; preserve the statement that this
installs definitions only and leaves timers disabled. In Section 2, make
`./scripts/setup-pgbackrest-r2.sh` the preferred path, retain the exact
five-option/permission/validator contract as a fallback explanation, and
link to the official R2 token documentation. Explicitly distinguish the
Cloudflare management token from the bucket-scoped R2 S3 credentials and say
that pgBackRest, not R2 lifecycle, owns retention.

Do not move or weaken the existing Section 9 timer activation commands or any
real-R2, disposable-expire, same-target baseline, isolated-restore, cleanup,
freshness, image, or Halt 2 gate. The runbook must continue to state that
timer activation after Section 9 is the only production-side activation.

- [ ] **Step 4: Link the repeatable path from the project README.**

Add an architecture/deployment bullet linking to
`infra/terraform/r2/README.md` and `docs/runbooks/offsite-dr.md`, stating that
Terraform manages only R2 infrastructure and the VM wizard manages runtime
secret injection. Do not put any account ID, token, endpoint value, or secret
in the README.

- [ ] **Step 5: Run focused tests and documentation checks.**

```bash
cd backend_py
uv run pytest tests/scripts/test_offsite_dr_ops.py tests/scripts/test_offsite_dr_bootstrap.py tests/scripts/test_offsite_dr_terraform.py -q
cd ..
git diff --check
```

Expected: all focused tests pass and the diff has no whitespace errors. Check
that Section 9 still appears after every backup/restore acceptance marker and
that no secret literal was introduced.

- [ ] **Step 6: Commit the runbook integration.**

```bash
git add docs/runbooks/offsite-dr.md backend_py/tests/scripts/test_offsite_dr_ops.py README.md
git commit -m "📝 Docs: wire offsite DR bootstrap runbook"
```

---

### Final verification after all tasks

- [ ] **Step 1: Run the complete repository checks from the required directory.**

```bash
cd backend_py
uv run pytest -m "not integration"
uv run mypy src/
uv run ruff check
cd ..
```

- [ ] **Step 2: Validate infrastructure and scripts without external side effects.**

```bash
cd infra/terraform/r2
terraform fmt -check
terraform init -backend=false
terraform validate
cd ../../..
bash -n scripts/setup-pgbackrest-r2.sh scripts/install-pgbackrest-timers.sh scripts/deploy-vm.sh deploy/vm/pgbackrest/backup.sh deploy/vm/pgbackrest/status.sh deploy/vm/pgbackrest/preflight.sh deploy/vm/pgbackrest/smoke.sh deploy/vm/pgbackrest/restore-drill.sh
git diff --check
```

- [ ] **Step 3: Request task/branch reviews before any integration.**

Each task receives a spec-compliance and quality review. After all tasks, run
the whole-branch review against the merge base. Resolve all Critical/Important
findings before handing the branch to the finishing workflow. Production
`terraform apply`, token creation, secret setup, timer activation, and PR/merge
remain separate operator actions.
