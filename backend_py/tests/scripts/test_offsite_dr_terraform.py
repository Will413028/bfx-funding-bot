"""Offline contracts for the secret-free offsite DR Terraform R2 module."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TERRAFORM_DIR = ROOT / "infra/terraform/r2"
MAIN_PATH = TERRAFORM_DIR / "main.tf"
OUTPUTS_PATH = TERRAFORM_DIR / "outputs.tf"
BACKEND_EXAMPLE_PATH = TERRAFORM_DIR / "backend.hcl.example"
TFVARS_EXAMPLE_PATH = TERRAFORM_DIR / "terraform.tfvars.example"
README_PATH = TERRAFORM_DIR / "README.md"

main_text = MAIN_PATH.read_text(encoding="utf-8")
lifecycle_text = main_text
outputs_text = OUTPUTS_PATH.read_text(encoding="utf-8")
backend_example_text = BACKEND_EXAMPLE_PATH.read_text(encoding="utf-8")
tfvars_example_text = TFVARS_EXAMPLE_PATH.read_text(encoding="utf-8")
readme_text = README_PATH.read_text(encoding="utf-8")
terraform_text = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(TERRAFORM_DIR.glob("*.tf"))
)


def test_module_declares_standard_backup_bucket_and_safe_lifecycle() -> None:
    """A wrong lifecycle rule could delete restorable pgBackRest objects."""
    assert 'resource "cloudflare_r2_bucket" "backup"' in main_text
    assert 'storage_class = "Standard"' in main_text
    assert 'resource "cloudflare_r2_bucket_lifecycle" "backup"' in lifecycle_text
    assert 'max_age = 86400' in lifecycle_text
    assert "abort_multipart_uploads_transition" in lifecycle_text
    assert "delete_objects_transition" not in lifecycle_text


def test_module_outputs_pgbackrest_s3_contract_without_runtime_secret() -> None:
    """pgBackRest needs stable non-secret S3 settings, never its key secret."""
    assert '"https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"' in outputs_text
    assert 'value       = "auto"' in outputs_text
    assert 'value       = "path"' in outputs_text
    assert 'value       = "/pgbackrest"' in outputs_text
    assert "secret_access_key" not in terraform_text
    assert "repo1-s3-key-secret" not in terraform_text


def test_module_does_not_manage_tokens_or_vm_files() -> None:
    """Terraform owns R2 infrastructure, not runtime credentials or VM state."""
    forbidden = ("cloudflare_api_token", "local_file", "remote-exec", "provisioner")
    assert not any(item in terraform_text for item in forbidden)


def test_state_example_is_separate_from_backup_bucket() -> None:
    """State and backup credentials must remain isolated by bucket boundary."""
    assert "bfx-funding-bot-terraform-state" in backend_example_text
    assert "bfx-funding-bot-pgbackrest" in tfvars_example_text
    assert "same bucket" in readme_text.lower()
    forbidden_assignments = (
        "AWS_SECRET_ACCESS_KEY",
        "CLOUDFLARE_API_TOKEN",
        "repo1-s3-key-secret",
        "repo1-cipher-pass",
    )
    assert not any(item in tfvars_example_text for item in forbidden_assignments)


def test_r2_backend_example_uses_documented_checksum_compatibility_setting() -> None:
    assert "skip_s3_checksum            = true" in backend_example_text


def test_module_readme_orders_bootstrap_apply_before_acceptance_gates() -> None:
    plan = readme_text.index("terraform plan -var-file=/secure/path/terraform.tfvars -out=/secure/path/r2.tfplan")
    show = readme_text.index("terraform show /secure/path/r2.tfplan")
    apply = readme_text.index("terraform apply /secure/path/r2.tfplan")
    gates = readme_text.index("R2 smoke", apply)

    assert plan < show < apply < gates
    assert "STOP: manually review" in readme_text
    assert "bootstrap apply" in readme_text
