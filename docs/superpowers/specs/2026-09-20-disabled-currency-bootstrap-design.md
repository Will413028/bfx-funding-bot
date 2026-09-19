# Disabled-currency bootstrap readiness

## Evidence and scope

The approved release bootstrap persisted an accepted complete account snapshot but then exited 2. A database-enforced read-only diagnostic of that historical observation confirmed fUST was valid and fUSD failed `snapshot_symbol_missing`. The account observation contained fUST only. Existing bootstrap fixtures supplied fUSD=0 and concealed this case. Conversion repeats the same unconditional preview dependency.

This is a bounded correction to the existing bootstrap/conversion interface, not a change to capital authorization. User has authorized completing technical deployment without intermediate approval. Production remains halted; this change never starts financial execution.

## Decision

Distinguish disabled policy configuration from enabled capital readiness. Bootstrap validates the enabled fUST capital view with canonical cell identity after full-account recovery. Conversion still writes an explicit disabled fUSD policy, but its report marks disabled cells as not evaluated, without fabricated balances, exposure, delta, or headroom. Enabled fUST remains subject to all existing repository guards. Keep account-wide offers/credits/wallet observations, stable confirmation, freshness, command fence, classification, unknown-execution checks and writer/halt requirements unchanged. Do not synthesize missing wallets or change CapitalRepository missing-symbol semantics.

Alternatives rejected: require depositing/transferring USD to instantiate a wallet (unnecessary financial action); default missing wallets to zero (turns absence into evidence); suppress all snapshot errors for disabled symbols after attempting preview (obscures readiness contract). Disabled conversion reports intentionally do not evaluate even an existing fUSD balance; this is a report of configuration, not a financial readiness report for that currency. Account-wide acceptance still validates all observed positions.

## Contract

- Python 3.13, pytest; no new dependencies or schema migration.
- fUST remains enabled and fully validated; fUSD remains explicitly disabled.
- Disabled conversion cells use `{"status": "disabled", "capital_evaluated": false}` only, no invented money values.
- Canonical cell IDs: fUST_a30, fUST_p2, fUSD_a30, fUSD_p2.
- No relaxation of CapitalRepository, account-wide observation, freshness, uncertainty, writer/halt or permit guards.
- No production mutation, financial API calls, authentication changes, secrets or image build by implementation agents.

## Verification

Demonstrate RED with a fUST-only wallet fixture, then GREEN for bootstrap and conversion. Verify explicit fUSD disabled policy, absent fUSD wallet remains absent in snapshot, no policy seeding by bootstrap and unchanged halt/permit state. Cover missing enabled fUST, stale/incomplete evidence and unknown execution as failures. Verify disabled reporting contains no financial fields and the dry/apply digest still binds enabled snapshot/policy/source. Run focused tests, actual PostgreSQL bootstrap integration tests, full non-integration pytest, ruff and mypy. New source requires review and a fresh immutable image pair before production retry.
