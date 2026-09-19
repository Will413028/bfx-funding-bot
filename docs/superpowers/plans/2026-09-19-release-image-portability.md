# Portable immutable image identity implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair actual classic-to-containerd deployment without weakening immutable release identity.

**Architecture:** Version the package/launch metadata, distinguish config/manifest/host identities, export one validated image per archive, and resolve only its bound immutable references on each engine. All existing technical and financial guards remain intact.

**Tech Stack:** Python3.13, pytest, Pydantic, stdlib tarfile/hashlib, existing Docker CLI; no new dependency or service.

**Spec:** docs/superpowers/specs/2026-09-19-release-image-portability-design.md

## Global Constraints

- Do not change capital policy, lending algorithms, authentication/MFA, permits, halt semantics, schema, existing PostgreSQL/Redis, hardware, Docker storage configuration or financial authorization.
- No production access by implementers/reviewers. Controller technical startup remains halted.
- Release metadata and launch receipt become version2; unsupported v1 metadata fails closed, with no fallback authority or silent migration.
- Export backend and frontend separately from their once-built images into two named tar artifacts.
- Runtime receives no Docker socket or host-only loader settings.
- Full backend pytest must pass before implementation commit; frontend code is not in scope.
- Do not overwrite or edit the old r3 bundle. It remains a failed deployment candidate.

### Task 1: Make packaging, host launch and runtime proof store-portable

**Files:**
- Create `backend_py/scripts/image_artifact.py` for bounded archive verification and immutable host resolution.
- Modify `backend_py/scripts/release_package.py`, `backend_py/scripts/immutable_release.py`, `backend_py/src/bfx_funding_bot/core/release_identity.py`, `backend_py/src/bfx_funding_bot/modules/execution/release_worker.py`.
- Test `backend_py/tests/scripts/test_image_artifact.py`, existing `test_release_package.py`, `test_immutable_deploy.py`, `test_release_packaging_container.py`, `test_release_container_migration.py`, `backend_py/tests/core/test_release_identity.py` and affected release integration fixtures.
- Update `docs/runbooks/immutable-release.md` and directly affected architecture/CLI test expectations; do not refactor unrelated modules.

**Interfaces:**
- Shared typed `PackagedImageIdentity` in core/release_identity: config_digest, manifest_digest, platform; strict SHA256 and supported-platform fields.
- `inspect_archive(path: Path) -> PackagedImageIdentity` in scripts/image_artifact verifies a single image's full descriptor/config/layer linkage without extracting paths. Preserve bounded metadata and streaming layer verification; reject duplicate/unsafe members.
- `resolve_image(identity: PackagedImageIdentity, runner) -> str` returns the exact inspected host engine ID only after strict role/platform verification. Runner is the existing captured-output callable; no raw Docker environment/error dumps.
- `VerifiedRelease` provides actual_image_id in addition to current manifest/release/config/launch properties; worker preflight consumes this actual deployed ID, not a mislabeled canonical digest.
- Version2 bundle records both artifact filenames/archive SHA256s and each image identity. Version2 launch receipt binds canonical identity and actual_image_id to inspected container metadata. Internal producer signatures may change together within this task; document exact final schema in the report/runbook.

- [ ] Write RED tests for the actual two-store mismatch and lossy pair export, plus repeatable cwd-safe source packaging. The regression must distinguish source config and target manifest IDs, not use the same fake digest for both. Example assertions:
  ```python
  identity = inspect_archive(single_backend_archive)
  assert identity.config_digest != identity.manifest_digest
  assert resolve_image(identity, containerd_runner) == identity.manifest_digest
  assert resolve_image(identity, classic_runner) == identity.config_digest
  with pytest.raises(PackagingBlocked):
      inspect_archive(ambiguous_pair_archive)
  ```
  Fixtures construct actual OCI JSON bytes, SHA256 descriptor values and tar members with matching layers/config; corrupt each independently for negative controls. Use real git in a temporary repository to prove root/backend cwd and deterministic archive mtime.
- [ ] Implement shared typed identity/version2 receipt validation and archive/resolver helper. Reject unsupported versions and unproven actual image IDs. Do not add a compatibility bypass or mutate old candidate metadata.
- [ ] Refactor producer to resolve git root/commit timestamp, build each component once, export separately, verify each archive before writing bundle, and only then publish final immutable metadata. Do not use docker export/import, which changes image metadata/content identity.
- [ ] Route all host CLI image consumers through the resolver and bind actual inspected Image into fresh launch receipt; runtime checks canonical identity and actual ID role. Update worker Halt2 image binding to VerifiedRelease.actual_image_id, maintaining its existing DR and runtime checks.
- [ ] Verify negative launch and runtime cases: wrong config/manifest/platform, mismatched archive digest, invalid descriptor size/content, missing layers, ambiguous/doubled metadata, wrong container.Image, unsupported v1 receipt and missing proof all reject before start. No tag-based or caller-claimed proof.
- [ ] Run focused pytest and existing actual readonly container/PG coverage for changed boundaries from backend_py. Use isolated test resources only, preserve unrelated Docker containers, and record clean teardown. No SSH/production/raw venue requests.
- [ ] Update the runbook with explicit source timestamp/root behavior, separate artifacts, target inspection and host ID versus content identity, preserving all existing halt/DR/role/human gates.
- [ ] Run `uv run ruff check` and `uv run mypy src/`; run `uv run pytest -m "not integration"` once after focused tests are green. Commit with Conventional Commits only after full pass; report failed and final runs distinctly.
- [ ] Write task report with RED/GREEN commands/results, exact schema/interfaces, source and archive contracts, coverage limits and clean commit/tree. Controller performs task and final review, then actual target acceptance; no implementer subagents.

### Task 2: Controller-only actual target acceptance

**Files:** No source changes; use approved producer/runbook and protected release receipts.

- [ ] After Task1 reviews and local merge, verify merged source, build a new approved pair once, independently verify source/archive/manifest digests, and transfer both archives under a new root-protected directory.
- [ ] Verify target load exposes both canonical image manifests/configs and host resolver returns exact actual IDs/platforms; do not swap storage drivers or rebuild on VM. This is required evidence missing from prior local-only gates.
- [ ] Resume unchanged schema dry-run/digest apply, readonly bootstrap, policy dry-run/apply, fresh quiescent backup/source/isolated restore and halted technical launch. Stop on genuine safety mismatch, never waive evidence. Human-only financial activation remains unperformed.
