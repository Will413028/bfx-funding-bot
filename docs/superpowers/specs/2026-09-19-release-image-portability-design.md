# Portable immutable image identity

## Scope and authority

This is a deployment defect discovered after the capital-policy branch was reviewed and merged at f22ccdc, during an actual transfer/load. It is not another fix wave for its closed review findings. The user authorized completing implementation and deployment without intermediate approval requests, and explicitly requested this retry. Implement this repair autonomously, but retain the human-only financial activation boundary.

Do not change capital policy, lending algorithms, authentication/MFA, permits, halt semantics, schema, existing PostgreSQL/Redis, hardware, Docker storage configuration or financial authorization. No production access by implementers/reviewers. Controller technical startup remains halted.

## Observed failure

Local Docker29.1.3 uses classic overlay2; target Docker29.5.2 uses io.containerd.snapshotter.v1. The reviewed source was built once, and the identical1515201536-byte archive was transferred and SHA256 verified. Local backend image/config ID is sha256:b59334f389a635a60362a7cba98b38982de563174a879e0df90d636643daba8b; target loaded OCI manifest ID sha256:b516196d95b32421c057aeecdb4068c2f94e74722c481f8cd155768ea4285869. Its manifest config descriptor points exactly to that original config ID. Neither --platform nor older Docker API makes the config ID addressable on target.

A second packaging defect exists: the Docker-save archive's Docker manifest.json describes the pair, but its OCI index.json contains only the backend descriptor. Containerd imported only that image. Do not assume a successful docker save/load carries both components.

## Alternatives and choice

1. Adopt explicit immutable content identities and resolve the actual host-engine ID after cryptographic archive validation (chosen). It supports both existing stores without changing infrastructure; costs a versioned metadata/receipt change and tests.
2. Change the VM or developer Docker store. Rejected: global state and existing workloads/volumes would be affected.
3. Substitute IDs in the old bundle or trust mutable tags. Rejected: it bypasses the reviewed binding and obscures provenance.

## Contract

- Release metadata and launch receipt become version2; unsupported v1 metadata fails closed, with no fallback authority or silent migration. No production release sessions currently exist, so no migration is needed. Historical test/operational evidence remains retained.
- Each component has an immutable typed identity: SHA256 config digest, SHA256 OCI image-manifest digest, and exact linux/arm64 or linux/amd64 platform. Digest roles are distinct; never call a config digest an OCI manifest digest or assume either is universally Docker's engine ID.
- Export backend and frontend separately from their once-built images into two named tar artifacts. Each archive must contain exactly the intended single image and a complete, verified config/manifest/layer relationship. Reject mismatched, missing, ambiguous, duplicate or unsafe metadata instead of selecting the first entry. Parse tar members without extracting attacker-controlled paths. Retain source/archive hashes for both components. Local names/tags, if required only to export, confer no authority.
- Resolve each loaded artifact to a host reference whose actual inspect ID is the expected config or expected manifest digest, with platform and descriptor role checks. Only the two cryptographically bound immutable identities are acceptable; no arbitrary tag, caller string, CLI wrapper that rewrites inspect output, or relaxed equality. Missing, unclassified Docker errors and mismatches fail closed.
- Every Docker operation (measurement, explicit migration, bootstrap, policy, database check, bot/API/FE create/start) uses the correct resolved reference. Created containers' actual Image must equal the resolver's inspected host ID before start.
- The host-protected launch receipt records the actual host ID plus the canonical packaged identity and binds the unchanged manifest digest, fresh launch ID, container ID, hostname and platform. Runtime verifies the packaged identity and actual ID role, full executable/config/Python inventory, protected paths and environment; no Docker socket or host-only loader settings enter runtime.
- VerifiedRelease exposes the verified host image ID separately. Existing Halt2/DR preflight uses that actual deployed/verifier ID, preserving compatibility with the restore verifier's current image-ID evidence. Session release/config binding stays tied to canonical release manifest and policy; changing artifact invalidates prior authority.
- All existing UID/read-only-root/secret exclusion/realm/operator/single-writer/UNKNOWN/once-only/no-retry guards remain unchanged. No schema or financial behavior change.

## Packaging reproducibility and operational contracts

Preparation must work from the documented backend_py directory as well as repo root, using an explicitly resolved git top-level for git archive and clean-tree checks. Use the reviewed commit timestamp for subtree archive mtime so source hashes reproduce; content still comes exclusively from git archive of the reviewed revision, not working files.

Do not overwrite or edit the old r3 bundle. It remains a failed deployment candidate. After tests/review/merge, build the new revision once into a new release directory, validate/export/load both archives, and prove actual target IDs before migration. Keep existing VM source/tooling/env files and stopped app recovery copies; do not switch storage drivers or create a registry/service.

## Acceptance

TDD must reproduce the real config-vs-manifest mismatch and missing-second-image index. Cover both engine response shapes, both components, root/backend cwd, repeatable source hashes, malformed/wrong-platform/config/layer/descriptor identities, container identity mismatch before start, receipt tampering and unsupported versions. Keep actual readonly container/PG regression coverage meaningful; fixtures must not merely echo supplied IDs. Full backend pytest must pass before implementation commit; frontend code is not in scope. Controller must load and inspect the final pair on the actual existing containerd target and compare archive content evidence before any migration/start. Technical health and human activation remain separate acceptance conditions.
