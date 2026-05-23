---
title: Axiom CI Integration + Observability Env Separation (T12 HIGH)
date: 2026-05-23
status: draft
phase: 4.4-prework
related-pending:
  - "(New 5/23) HIGH — 真把 T12 (AxiomReplayQueryAdapter integration test) 跑起來"
  - "(New 5/23) Pydantic `extra='ignore'` 在 replay 路徑 (separate follow-up, not bundled)"
  - "#4 OpenTelemetry adoption (L3 future, this spec is OTEL-aligned pre-step)"
related-spec:
  - docs/superpowers/specs/2026-05-22-phase4.3-executor-middleware-design.md
  - docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md
---

# Axiom CI Integration + Observability Env Separation

## Context

### 觸發

Phase 4.4b prework deploy（5/23）連環踩 2 個 wire-level bug 才 surface 到 prod：

| Commit | Bug | 為什麼 unit test 沒抓 |
|---|---|---|
| `03e3486` | `AxiomReplayQueryAdapter` APL 有 `project ['payload', ...]` clause → Axiom flatten nested objects → 真 dataset 回 HTTP 400 | Unit test 只 mock httpx Response 驗 APL string structure，不打真 Axiom |
| `0447e29` | Axiom columnar union schema：query `order_fill` 回的 row 帶其他 event types 的 `payload.*` field with `None` value → parser re-nest 後丟給 `OrderFillPayload(extra='forbid')` 引發 44 ValidationError | 同上，unit test 餵的 mock response 是 clean OrderFill schema |

兩個 bug 都是 **wire protocol level**：Axiom 真實 HTTP response shape 跟 mock 不一樣。T12（`backend_py/tests/integration/test_axiom_replay_query_real.py`）原本就是設計來 catch 這類 bug —— 但因為環境變數 `AXIOM_API_KEY` + `AXIOM_DATASET` 在 CI 沒設，`skipif` 跳過。CI 完全沒 backend_py job，所以連 unit test 都沒在 PR / push 跑。

bfx-bot 目前 Phase 4.4 將進 real-money canary（$150-$500 allocation），「wire-level bug 上 prod 才 surface」風險不可接受。

### 為什麼這個 spec 同時做 observability env separation

現況：Koyeb shadow service 寫到 `bfx-funding-bot` dataset（即 prod dataset）。Phase 4.4 canary 啟動後，real-money events 會跟 paper/shadow events 混在同一 dataset：
- G3 P&L tracking error gate 數字會被 shadow 噪音稀釋
- Alert routing 無法區分（shadow alert 不該觸發 oncall）
- Retention / quota 共享，CI noise 吃 prod budget

**「現在系統還沒正式上線」是分層 observability 的最低成本時機**。等 4.4 canary 開始累積 real-money events 後再分，要做 dataset migration 風險更高。

Trigger 是 T12，但解的是更廣的 problem：**emitter-side env separation + receiver-side env filter，雙端對稱，pre-real-money 一次做對**。

### 業界 best practice alignment

| 抽象層 | 業界 reference | 本 spec 對齊處 |
|---|---|---|
| Per-env data isolation | Datadog / Honeycomb / AWS CloudWatch Logs / Stripe | 3 dataset 拆 prod / shadow / ci |
| Token scoping per env | AWS IAM scoped to log group / Datadog API key scoped + tag-based ACL | Axiom dataset-scoped API key（native 支援） |
| Resource attribute model | OpenTelemetry `deployment.environment.name` semantic convention | `EventResource` dataclass + envelope-level field（非 payload-level） |
| Service version tagging | OTEL `service.version` semantic convention | `EventResource.service_version = $GIT_SHA`（build-time embed） |
| Per-run isolation in integration test | Stripe `req_*` request ID / Plaid sandbox access_token | `account_id=f"ci-{GITHUB_RUN_ID}-{commit_sha[:8]}"` |
| CI trigger pattern | Trunk-based: PR + main push | PR + main push 都跑 |
| Concurrency control | GitHub Actions `concurrency.cancel-in-progress` per branch | PR 啟用，main push 不 cancel |
| Test isolation polling | Stripe webhook integration test / Plaid sandbox | Retry-with-backoff query loop (not fixed sleep) |

OpenTelemetry SDK 完整採用是 Pending #4 L3 phase（~2-3 day）。本 spec 走 **OTEL-inspired Resource layer**：建立 `EventResource` dataclass + semantic convention 對齊欄位名，未來 OTEL migration 是 1-1 mapping、不需動 schema / dashboard / alert query。

## Goals

Measurable success criteria — ship 後 30 天內可逐項勾選驗證：

1. **G1：Wire-level bug catch rate**：T12 跑通後，未來 `AxiomReplayQueryAdapter` 寫法改動 PR 階段就 catch、不再上 prod surface（5/23 兩個 bug 是基準線 0%；目標 100%）
2. **G2：CI feedback latency**：PR push → `backend_py_integration` 結果 ≤ 90s（unit ~30s + integration ~30-60s 含 Axiom indexing polling）
3. **G3：Cross-run isolation**：並行 CI run（PR re-push + main push 同時跑）零 cross-pollution，每 run 只 query 到自己 emit 的 events
4. **G4：Env separation 完整性**：Phase 4.4 canary 啟動後，`bfx-funding-bot` prod dataset 100% 純淨（無 shadow / ci event mix）— Axiom query `where deployment_environment != "prod"` 0 rows
5. **G5：OTEL migration readiness**：未來啟動 OTEL adoption phase 時，`EventResource.to_otel_resource()` 直接呼叫產出標準 OTEL Resource，dashboard / alert query 零改動
6. **G6：Schema evolution readiness**：未來改 envelope schema（加 field / rename）時，`schema_version` discriminator 讓 replay 走 version-aware parser dispatch、不破舊 events
7. **G7：CI workflow 安全性**：通過 OpenSSF Scorecard `Pinned-Dependencies` + `Token-Permissions` 基本檢查（permissions block + timeout-minutes + Actions pin-by-SHA via Dependabot）

每個 Goal 在 Acceptance Criteria 對應具體勾選項。

## Decision Summary

| # | Decision | Why |
|---|---|---|
| D1 | **3 Axiom dataset**：`bfx-funding-bot`（prod / 4.4 canary）、`bfx-funding-bot-shadow`（paper + shadow）、`bfx-funding-bot-ci`（CI integration + 本機 dev） | Per-env data isolation 業界標準；real-money G3 P&L 不被稀釋 |
| D2 | **API key per dataset**（dataset-scoped Axiom token） | 誤上 prod 物理上不可能；secret rotation 風險 bounded |
| D3 | **`EventResource` dataclass + envelope-level field（非 payload）** | OTEL Resource pattern，所有 event types 自動帶，零 Pydantic model 改動 |
| D4 | **Field name 對齊 OTEL semantic conventions**：`deployment_environment` / `service_name` / `service_version` | 未來 OTEL migration 1-1 mapping，dashboard / alert query 不需改 |
| D5 | **Env var：`BFX_DEPLOYMENT_ENV`**（vendor-neutral，不 prefix `AXIOM_`） | env 是 universal concept、不 bind vendor |
| D6 | **`DeploymentEnvironment` StrEnum**（`PROD` / `SHADOW` / `CI`） | Type-safe、IDE auto-complete、exhaustive match、SoT |
| D7 | **`pydantic-settings.BaseSettings` 取代 `os.environ[]`** | Auto env binding、fail-loud with helpful error |
| D8 | **CI trigger：PR + main push 都跑**；Phase 4.4 cutover 前升 required merge gate | trunk-based standard；wire-level bug 在 PR 階段 catch 最便宜 |
| D9 | **Concurrency：PR `cancel-in-progress: true`、main push 不 cancel** | 省 Axiom CI dataset quota；main push 不打斷 deploy log |
| D10 | **Integration job `needs: backend_py_unit`** | fail-fast；unit fail 不浪費 Axiom quota |
| D11 | **Fork PR 自動 skip integration**（GitHub default + explicit `if:` 表達 intent） | Secret 不外洩 |
| D12 | **Test isolation：UUID-scoped `account_id=f"ci-{GITHUB_RUN_ID}-{commit_sha[:8]}"` + per-event `signal_correlation_id=uuid4()`** | 並行 CI run 不互相 pollute；不靠 mutex serialize PR queue |
| D13 | **T12 polling-based wait（取代 fixed `time.sleep(5)`）** | Axiom indexing latency 不 deterministic (observed 2-15s)；polling robust 又快 |
| D14 | **Pydantic `extra='ignore'` 不 bundle 進這個 spec** | T12 spec 是 CI infra；Pydantic patch 是 source code defensive change。分開 review。Spec ship 後 immediate follow-up |
| D15 | **5 個 pre-existing integration failure 用 `xfail(strict=False)` 標** | 不混 yak shave；意外 pass 會 warn 暴露「修好沒人發現」case |
| D16 | **Axiom CI dataset retention 3-7d（非預設 30d）** | CI 量小 + lifecycle 短；省 quota；不影響 wire-level bug catch |
| D17 | **Pre-cutover 舊 shadow events 留 prod dataset，靠 30d retention 自清** | 不 backfill / 不刪；Axiom schemaless 自然兼容；30d 後 prod dataset 100% 純淨 |
| D18 | **EventResource 加 `schema_version: int = 1` 第 4 個 envelope field** | Event-sourcing 標準（EventStore / Kafka schema registry / AWS EventBridge）；未來 envelope schema migration 不破舊 replay；零 runtime cost；4.4 canary 前 ship 是最低成本時機 |
| D19 | **EventResource 加 `host_name: str | None` Optional 欄位** | Pending I3 chaos log 已顯示 Koyeb instance ID 對 debug 重要；零 runtime cost；多 instance canary/control 平行跑時直接可用 |
| D20 | **EventResource 提供 `to_otel_resource()` migration helper** | 未來 OTEL adoption phase 直接呼叫產出標準 `opentelemetry.sdk.resources.Resource`，無 schema 翻譯成本 |
| D21 | **CI workflow `permissions: contents: read` + `timeout-minutes: 10` per job + Actions SHA pin via Dependabot** | OpenSSF Scorecard 對齊；real-money repo 安全 hardening；least-privilege GITHUB_TOKEN；防 runaway CI |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ Axiom Org (existing)                                                │
│                                                                     │
│  ┌────────────────────────┐ ┌──────────────────────┐ ┌────────────┐ │
│  │ bfx-funding-bot        │ │ bfx-funding-bot-     │ │ bfx-       │ │
│  │ (prod / 4.4 canary)    │ │ shadow (paper/shadow │ │ funding-   │ │
│  │ retention: 30d         │ │ run, retention: 30d) │ │ bot-ci     │ │
│  │ dataset-scoped token A │ │ dataset-scoped token │ │ retention: │ │
│  │                        │ │ B                    │ │ 3-7d       │ │
│  └──────────▲─────────────┘ └─────────▲────────────┘ │ token: C   │ │
│             │                         │              └──────▲─────┘ │
└─────────────┼─────────────────────────┼─────────────────────┼───────┘
              │ AXIOM_API_KEY=A         │ AXIOM_API_KEY=B     │
              │ AXIOM_DATASET=...       │ AXIOM_DATASET=...   │ AXIOM_API_KEY=C
              │ BFX_DEPLOYMENT_ENV=prod │ BFX_DEPLOYMENT_ENV  │ AXIOM_DATASET=...
              │                         │   =shadow           │ BFX_DEPLOYMENT_ENV=ci
   ┌──────────┴───────────┐  ┌──────────┴──────────┐  ┌───────┴─────────┐
   │ Koyeb prod service   │  │ Koyeb shadow svc    │  │ GitHub Actions  │
   │ (4.4 canary, future) │  │ (current paper run) │  │ (PR + main push)│
   └──────────────────────┘  └─────────────────────┘  └─────────────────┘
                                                              │
                                                              │ uv run pytest -m integration
                                                              ▼
                                              T12: test_axiom_replay_round_trip
                                              + future Axiom integration tests
```

### Event envelope shape（after spec）

```python
{
  "_time": 1716480000000,                          # existing
  "type": "order_fill",                            # existing
  "schema_version": 1,                             # NEW D18 — event-sourcing standard, version-aware replay future-proof
  "deployment_environment": "ci",                  # NEW D3 (OTEL: deployment.environment.name)
  "service_name": "bfx-funding-bot",               # NEW D3 (OTEL: service.name)
  "service_version": "0447e29a",                   # NEW D3 (OTEL: service.version, $GIT_SHA)
  "host_name": "marketfeed-abc123",                # NEW D19 Optional (OTEL: host.name, Koyeb $HOSTNAME)
  "payload": { ...unchanged... },                  # existing
}
```

Payload schemas（`OrderFillPayload` / `ReservationClaimedPayload` / `ReservationReleasedPayload`）**完全不動**。

## Source Code Changes

### 1. New module: `src/bfx_funding_bot/modules/observability/resource.py`

```python
import os, subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

ENVELOPE_SCHEMA_VERSION: int = 1  # bump on breaking envelope shape change

class DeploymentEnvironment(StrEnum):
    PROD = "prod"
    SHADOW = "shadow"
    CI = "ci"

def _resolve_service_version() -> str:
    """Resolve service version from build-time env or git fallback.

    Build: Dockerfile ARG GIT_SHA → ENV BFX_SERVICE_VERSION
    Local dev: git rev-parse HEAD fallback
    """
    v = os.environ.get("BFX_SERVICE_VERSION")
    if v:
        return v
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"

def _resolve_host_name() -> str | None:
    """Koyeb sets HOSTNAME to the instance ID; None locally is fine."""
    return os.environ.get("HOSTNAME")

@dataclass(frozen=True)
class EventResource:
    """Process-level immutable attributes attached to every event.

    Aligned with OpenTelemetry Resource semantic conventions:
    - deployment.environment.name → deployment_environment
    - service.name → service_name
    - service.version → service_version
    - host.name → host_name (Optional, Koyeb instance ID)
    """
    deployment_environment: DeploymentEnvironment
    service_name: str = "bfx-funding-bot"
    service_version: str = field(default_factory=_resolve_service_version)
    host_name: str | None = field(default_factory=_resolve_host_name)
    schema_version: int = ENVELOPE_SCHEMA_VERSION

    def envelope_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "schema_version": self.schema_version,
            "deployment_environment": self.deployment_environment.value,
            "service_name": self.service_name,
            "service_version": self.service_version,
        }
        if self.host_name is not None:
            fields["host_name"] = self.host_name
        return fields

    def to_otel_resource(self) -> Any:
        """D20 migration helper — produces opentelemetry.sdk.resources.Resource.

        Only imported when called (lazy) — no OTEL SDK runtime dependency until adoption phase.
        """
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]

        attrs: dict[str, Any] = {
            "deployment.environment.name": self.deployment_environment.value,
            "service.name": self.service_name,
            "service.version": self.service_version,
        }
        if self.host_name is not None:
            attrs["host.name"] = self.host_name
        return Resource.create(attrs)
```

`to_otel_resource()` 是 D20 migration helper：未來 OTEL adoption phase 啟動時，emitter 從 Axiom HTTP 改 OTEL Exporter，這個 method 直接呼叫產出標準 OTEL Resource，semantic convention 對應一致、零 schema 翻譯成本。Import 走 lazy（method 內），不需要現在加 OTEL SDK 依賴。

### 2. `modules/marketfeed/config.py` — `AxiomConfig` 加 deployment_env

兩種 acceptable impl shape（impl 階段擇一，依既有 config 模組 pattern 對齊；不阻塞 plan）：

**Shape A — 升 BaseSettings**（若既有 config 已用 pydantic-settings 或可接受新依賴）：
```python
class AxiomConfig(BaseSettings):
    api_key: str
    dataset: str
    deployment_env: DeploymentEnvironment
    # ...existing fields unchanged...
```

**Shape B — 保持 BaseModel + classmethod 讀 env**（若既有 config 是 BaseModel，避免引入 BaseSettings 依賴）：
```python
class AxiomConfig(BaseModel):
    api_key: str
    dataset: str
    deployment_env: DeploymentEnvironment
    # ...existing fields unchanged...

    @classmethod
    def from_env(cls) -> "AxiomConfig":
        env_val = os.environ.get("BFX_DEPLOYMENT_ENV")
        if not env_val:
            raise ValueError("BFX_DEPLOYMENT_ENV required (one of: prod, shadow, ci)")
        return cls(
            api_key=os.environ["AXIOM_API_KEY"],
            dataset=os.environ["AXIOM_DATASET"],
            deployment_env=DeploymentEnvironment(env_val),
        )
```

兩 shape 對外契約等價（`AxiomConfig` 有 `deployment_env` field + 缺 env var fail-loud + invalid env value raise）。D7 BaseSettings 是 best practice 偏好但若引入 dep 成本高則 Shape B 等價可接受。Unit test 鎖契約兩 shape 通用。

### 3. `modules/observability/axiom_client.py`（or wherever `AxiomClient._build_event` lives）

```python
class AxiomClient:
    def __init__(self, *, config: AxiomConfig, resource: EventResource, ...):
        self._config = config
        self._resource = resource
        # ...

    def _build_envelope(self, event_type: str, payload: dict, _time_ms: int) -> dict:
        return {
            "_time": _time_ms,
            "type": event_type,
            **self._resource.envelope_fields(),
            "payload": payload,
        }
```

### 4. `modules/execution/axiom_event_query.py` — adapter 加 deployment_environment filter

```python
class AxiomReplayQueryAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        dataset: str,
        deployment_environment: DeploymentEnvironment,  # NEW
        base_url: str = "https://api.axiom.co",
        timeout: float = 30.0,
    ):
        self._deployment_environment = deployment_environment
        # ...existing init...

    def _build_apl(self, *, account_id: str, since: datetime) -> str:
        return f"""
        ['{self._dataset}']
        | where _time > datetime({since.isoformat()})
        | where deployment_environment == "{self._deployment_environment.value}"
        | where account_id == "{account_id}"
        | order by _time asc
        """
```

### 5. `daemon.py build_daemon` — 雙端對稱 wire

```python
def build_daemon(config: AxiomConfig, ...) -> Daemon:
    resource = EventResource(deployment_environment=config.deployment_env)
    axiom_client = AxiomClient(config=config, resource=resource, ...)
    axiom_replay_adapter = AxiomReplayQueryAdapter(
        api_key=config.api_key,
        dataset=config.dataset,
        deployment_environment=config.deployment_env,  # 必須跟 client.resource 同 env
        ...
    )
    # ...
```

**Invariant**：`axiom_client.resource.deployment_environment == axiom_replay_adapter.deployment_environment`。Unit test 鎖此契約。

### 6. `tests/integration/test_axiom_replay_query_real.py` — T12 升級

```python
import asyncio, os, time
from uuid import uuid4
import pytest

@pytest.mark.integration
@pytest.mark.skipif(not _AXIOM_AVAIL, reason="AXIOM creds not set")
async def test_axiom_replay_round_trip():
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    sha = os.environ.get("GITHUB_SHA", "local")[:8]
    run_account_id = f"ci-{run_id}-{sha}"
    test_start = datetime.now(UTC)

    # Build resource + client + adapter (production code path)
    resource = EventResource(deployment_environment=DeploymentEnvironment.CI)
    client = AxiomClient(config=cfg, resource=resource, ...)
    adapter = AxiomReplayQueryAdapter(
        api_key=cfg.api_key, dataset=cfg.dataset,
        deployment_environment=DeploymentEnvironment.CI,
    )

    # Emit 3 events with unique correlation_id
    cid1, cid2, cid3 = uuid4(), uuid4(), uuid4()
    await client.emit(ReservationClaimedEvent(account_id=run_account_id, payload={"cid": 1, "signal_correlation_id": cid1, ...}))
    await client.emit(OrderFillEvent(account_id=run_account_id, payload={"cid": 1, "signal_correlation_id": cid2, ...}))
    await client.emit(ReservationReleasedEvent(account_id=run_account_id, payload={"cid": 1, "signal_correlation_id": cid3, ...}))
    await client.flush()

    # Polling wait (replaces fixed sleep(5))
    rows = await _poll_until_indexed(adapter, run_account_id, test_start, expected_count=3, timeout_s=30, interval_s=1.0)

    # Assertions
    types = {r["type"] for r in rows}
    assert types == {"reservation_claimed", "order_fill", "reservation_released"}
    assert all(r["deployment_environment"] == "ci" for r in rows)  # D3
    assert all(r["schema_version"] == 1 for r in rows)             # D18
    assert all(r["service_name"] == "bfx-funding-bot" for r in rows)  # D3
    assert all(r["service_version"] for r in rows)                 # D3 — non-empty (GitHub sha)
    rc = next(r for r in rows if r["type"] == "reservation_claimed")
    assert rc["payload"]["cid"] == 1

async def _poll_until_indexed(adapter, account_id, since, *, expected_count, timeout_s, interval_s):
    deadline = time.monotonic() + timeout_s
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = await adapter.query_order_events(account_id=account_id, since=since)
        if len(rows) >= expected_count:
            return rows
        await asyncio.sleep(interval_s)
    raise AssertionError(
        f"Axiom indexing timeout: got {len(rows)}/{expected_count} after {timeout_s}s "
        f"(account_id={account_id})"
    )
```

### 7. `Dockerfile` — propagate `$GIT_SHA` 為 `BFX_SERVICE_VERSION`

```dockerfile
ARG GIT_SHA=unknown
ENV BFX_SERVICE_VERSION=${GIT_SHA}
```

Koyeb / GitHub Actions build 命令傳 `--build-arg GIT_SHA=$(git rev-parse --short HEAD)`。

### 8. Pre-existing integration failures — `xfail(strict=False)` 標記

5 個 test 各加 marker + reason 指向 Pending follow-up：

```python
@pytest.mark.xfail(
    strict=False,
    reason="SQLite/JSONB infra mismatch; tracked in Pending '5 個 pre-existing integration failure 修'",
)
def test_daemon_shutdown_clean(): ...
```

不修這 5 個 test 是 D15 的明示 non-goal。

## CI Workflow

完整 `.github/workflows/ci.yml` 增量（既有 `backend` / `frontend` job 不動）。Hardening 對齊 OpenSSF Scorecard / GitHub Actions security guide：

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

# Workflow-level default — narrowed per-job if needed
permissions:
  contents: read

jobs:
  # existing: backend (Go, archived), frontend (Next.js) — unchanged

  backend_py_unit:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    defaults:
      run:
        working-directory: backend_py
    steps:
      # NOTE: Actions pinned to commit SHA via Dependabot (D21).
      # During plan execution use latest SHA + add to .github/dependabot.yml
      # for auto-bump PR. Until first Dependabot scan, version tags
      # (@v4 / @v3) are acceptable per OpenSSF Scorecard transitional guidance.
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - run: uv sync --all-groups
      - run: uv run ruff check
      - run: uv run mypy --strict src
      - run: uv run pytest -m "not integration" -q

  backend_py_integration:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
    needs: backend_py_unit
    if: github.event.pull_request.head.repo.full_name == github.repository || github.event_name == 'push'
    defaults:
      run:
        working-directory: backend_py
    env:
      AXIOM_API_KEY: ${{ secrets.AXIOM_CI_API_KEY }}
      AXIOM_DATASET: ${{ secrets.AXIOM_CI_DATASET }}
      BFX_DEPLOYMENT_ENV: ci
      BFX_SERVICE_VERSION: ${{ github.sha }}
      GITHUB_RUN_ID: ${{ github.run_id }}
      GITHUB_SHA: ${{ github.sha }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - run: uv sync --all-groups
      - run: uv run pytest -m integration -q --maxfail=3
```

### `.github/dependabot.yml` 增量

啟用 Dependabot 自動管理 Actions SHA pin（D21）：

```yaml
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
```

第一次 Dependabot scan 跑完後，Actions 自動 PR 升 SHA pin（例 `actions/checkout@<40-char-sha>`），人類 review 後 merge。Maintenance burden 由 Dependabot 承擔。

### Security 決策說明

- **GitHub Environments scope**：本 spec 用 **repo-level secrets**（非 environment-scoped）。CI dataset 風險低（無 prod data write 權限、retention 3-7d 自清），不需 environment required reviewer / branch protection。未來若 secret rotation 流程涉及多人 approval，再升 GitHub Environments
- **GITHUB_TOKEN permissions**：workflow-level `permissions: contents: read` + per-job 明示 `permissions: contents: read`。Job 不需 `write`（不 push tag / 不 comment PR / 不 release）
- **timeout-minutes: 10**：unit ~1min、integration ~2-3min 含 Axiom indexing polling 30s upper bound，10min 是 ~3x headroom 防 runaway
- **Axiom OIDC token federation**：Axiom 目前不支援 OIDC（最後 verify 在 2026-05），long-lived dataset-scoped token 是 only option。Open Question 追蹤未來 Axiom 是否新增 OIDC 支援

### GitHub Actions repo secrets

| Secret name | Value | Scope |
|---|---|---|
| `AXIOM_CI_API_KEY` | Axiom token，dataset-scoped 限 `bfx-funding-bot-ci` write + query | Repository secret |
| `AXIOM_CI_DATASET` | `bfx-funding-bot-ci`（literal string） | Repository secret |

**Dataset name 也放 secret 而非 hardcode**：跟 API key 一起 rotate / migrate 時 1 處改完搞定，workflow YAML 不需改。

### Phase 4.4 cutover 前升 required merge gate

GitHub Settings → Branches → Add rule → `main` branch protection → require `backend_py_integration` status check pass。明示在 Phase 4.4 cutover prework checklist。

## Migration Sequence

| Step | Action | Verify command / observation |
|---|---|---|
| 1 | Axiom console 手動建 2 dataset：`bfx-funding-bot-shadow`（retention 30d）+ `bfx-funding-bot-ci`（retention 3-7d） | `curl -H "Authorization: Bearer $shadow_key" https://api.axiom.co/v1/datasets/bfx-funding-bot-shadow/ingest -d '[{"_time":"...","type":"ping"}]'` → 200 OK |
| 2 | 生成 2 個 dataset-scoped API key（shadow + ci） | Axiom console → token list |
| 3 | Source code change ship（本 spec 改動 1-8） | Koyeb deploy log: `AxiomConfig(deployment_env=...)` 出現 / Axiom prod dataset 開始看到 events 有 `deployment_environment` field |
| 4 | Koyeb shadow service env 更新 | Koyeb console: `AXIOM_DATASET=bfx-funding-bot-shadow` + `AXIOM_API_KEY=<shadow_key>` + `BFX_DEPLOYMENT_ENV=shadow` |
| 5 | Verify shadow 切換 | 等下個 hour tick（5-15min observe window），Axiom shadow dataset 收到 candle / signal events / prod dataset 不再收新 events |
| 6a | GitHub repo secrets 先設定（必須在 6b workflow merge 前完成，否則第一次 PR push integration job 即 fail） | GitHub Settings → Secrets 列表確認 `AXIOM_CI_API_KEY` + `AXIOM_CI_DATASET` 存在 |
| 6b | CI workflow PR merge | 第一次 PR push 觸發 `backend_py_integration` green / Axiom ci dataset 收到 test events |
| 7 | 本機 dev `.env` runbook 更新 | `BFX_DEPLOYMENT_ENV=ci` + `AXIOM_DATASET=bfx-funding-bot-ci` + ci API key；`uv run pytest -m integration` pass |
| 8 | Phase 4.4 cutover prework checklist 加項 | Spec 標明：確認 `bfx-funding-bot` dataset 過 30d 已純淨 + GitHub branch protection 啟用 `backend_py_integration` required check |

### Rollback plan

| Failure point | Rollback action | RTO |
|---|---|---|
| Step 3 source code ship 後 daemon 啟動失敗 | `git revert` + Koyeb auto-redeploy 回舊 code（field 缺也不會破 Axiom emit） | ~5 min |
| Step 4 Koyeb env 切換後 shadow daemon 寫不進新 dataset | Koyeb console 還原 env vars | ~5 min |
| Step 6 CI 啟用後 T12 持續 fail（Axiom CI dataset wire issue） | Workflow yaml 改 `if: false` 暫關 integration job、PR 仍可 merge unit job pass 即可 | 即時 |

## Risks & Failure Modes

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Axiom indexing latency spike（>30s polling timeout） | LOW | T12 false negative，PR 階段 block | Polling timeout 設 30s = ~2x 觀察到的 worst case；timeout 時 fail message 帶 partial rows for debug；可調 timeout via env var |
| R2 | 並行 CI run cross-pollution（D12 isolation 漏網） | LOW | T12 false positive（看到別 run 的 events） | account_id 含 `GITHUB_RUN_ID` + commit SHA、每 event 獨立 `signal_correlation_id` UUID；assert 用嚴格 set 比較 |
| R3 | Koyeb shadow service Step 4 env 切換 → daemon 啟不來（misconfig） | MED | Shadow run 中斷直到 fix | Koyeb 切換前先在 Koyeb console preview env diff；切換後 5 min 內監看 deploy log；自動 rollback via Koyeb console UI |
| R4 | Pre-cutover prod dataset 內舊 events 沒 `deployment_environment` field，replay 拿不到 | MED | Phase 4.4 cutover 前期 ledger replay 部分跳掉舊 events | 4.4 canary 啟動前等 prod dataset 30d retention 自然清完（cutover 日 + 30 day = canary 啟動最早日期）；spec 明示在 Phase 4.4 cutover prework checklist |
| R5 | Axiom CI dataset retention 3-7d 導致 debug 時舊 CI fail run 已清 | LOW | 重現 CI bug 困難 | Failed CI run log 保留 GitHub Actions artifact 90d；Axiom dataset 只是 sample data 不是 source of truth |
| R6 | OTEL SDK 未安裝時 `to_otel_resource()` 被誤呼叫 | LOW | Runtime ImportError | Lazy import + method docstring 明示「OTEL adoption phase 後才呼叫」；unit test 用 mock module |
| R7 | Dependabot SHA-pin PR 量太大壓垮 review queue | LOW | Maintenance burden 上升 | Dependabot config 設 weekly + 限 max 5 open PR；group by ecosystem |
| R8 | T12 emit fail（Axiom CI dataset 滿 quota / network outage）誤判 wire-level bug | LOW | False alarm | Polling fail message 區分 "emit failed" vs "query timeout" vs "0 events returned"；Axiom quota alert 設在 Axiom console |
| R9 | `schema_version=1` ship 後未來真改 envelope schema 時，replay 沒寫 dispatch 邏輯 | MED | 改 schema 時忘記 bump version + 加 version-aware parser | 加 docstring + ADR 後續 follow-up「envelope schema change checklist」；本 spec 不加 dispatch 邏輯（明示 non-goal） |

對應 mitigation 在 plan 階段分配到具體 task。

## Open Questions

不阻塞 plan，impl 階段或 ship 後 30d 內 clarify：

| # | Question | 影響 | 何時解 |
|---|---|---|---|
| OQ1 | `AxiomConfig` 既有實作是 BaseModel 還是 BaseSettings？ | 決定 Shape A vs Shape B（Source Code Changes section 2 已列兩 acceptable shape） | Plan 第 1 task 開檔即知 |
| OQ2 | `AxiomClient._build_event` 確切 method name + signature？ | 決定 Resource 注入點具體 patch shape | Plan 第 2 task 開檔即知 |
| OQ3 | 既有 Koyeb shadow service Axiom dataset secret 是手動設還是 CI 自動同步？ | 決定 Migration Step 4 操作模式 | Cutover day 前 Koyeb console 查 |
| OQ4 | Axiom 未來是否新增 OIDC token federation 支援？ | 影響 D2 long-lived token vs short-lived token rotation 策略 | 6-12 month 持續追蹤 Axiom changelog；Spec ship 後 30d 內 file Axiom support ticket 問 roadmap |
| OQ5 | Phase 4.4 canary 啟動後是否需要 `host_name` 區分 instance？ | 決定 `service.instance.id` 何時加入 EventResource | 4.4 canary 啟動後第 1 週實際觀察 |
| OQ6 | `--build-arg GIT_SHA` 在 Koyeb build pipeline 怎麼 propagate？ | 決定 Dockerfile + Koyeb config 改動範圍 | Plan Dockerfile task 前 Koyeb docs 確認 |
| OQ7 | 5 個 pre-existing integration failure xfail 後若意外 pass，CI policy 是 warn 還是 fail？ | 影響 `strict=False` vs `strict=True` 選擇 | 本 spec 暫定 `strict=False`，ship 後 30d 內若意外 pass case 出現再決策 |

## Testing Strategy

### Test pyramid

```
                              ┌──────────────────────────────┐
                              │ T12 round-trip (1 test)      │
                              │ Real Axiom CI dataset        │
                              │ Wire-level + indexing latency│
                              └──────────────────────────────┘
                              ┌──────────────────────────────┐
                              │ Adapter HTTP unit tests      │
                              │ Mock httpx Response          │ ← 既有 test_axiom_event_query.py
                              │ APL string + parser contract │
                              └──────────────────────────────┘
                              ┌──────────────────────────────┐
                              │ Pure unit tests              │
                              │ EventResource / StrEnum /    │
                              │ AxiomClient envelope build / │
                              │ AxiomSettings env binding    │
                              └──────────────────────────────┘
```

### 新增 / 修改的 unit tests

| Test file | 鎖什麼契約 |
|---|---|
| `tests/unit/observability/test_event_resource.py`（new） | `EventResource` immutability（frozen=True 違反 raise）/ `envelope_fields()` 鍵名對齊 OTEL semantic conventions / 含 `schema_version=1` / `host_name` 出現 iff non-None / `DeploymentEnvironment` enum exhaustive / `_resolve_service_version` build-env 優先 / git fallback / unknown fallback / `_resolve_host_name` 拿 `HOSTNAME` env 或 None / `to_otel_resource()` 產出對應 OTEL semantic convention key（mock OTEL SDK if not installed） |
| `tests/unit/marketfeed/test_axiom_config.py`（new 或擴充） | `AxiomConfig` 缺 `BFX_DEPLOYMENT_ENV` env 時 fail-loud（Pydantic ValidationError）/ 三種 env value 都 accept / 其他 string raise |
| `tests/unit/observability/test_axiom_client_resource.py`（new） | AxiomClient `_build_envelope` 注入 resource fields 在 top-level（非 payload 內） / 既有 payload schema 不被污染 / `type` + `_time` 仍存在 |
| `tests/unit/execution/test_axiom_event_query.py`（擴充既有） | APL string 包含 `where deployment_environment == "{env}"` clause / Adapter `__init__` accepts deployment_environment + propagates 進 `_build_apl` / mock response 解析正確 |
| `tests/unit/test_daemon_axiom_wiring.py`（new 或擴充） | `build_daemon` 把 `config.deployment_env` 同時 wire 進 `axiom_client.resource` 跟 `axiom_replay_adapter.deployment_environment` — 兩端對稱 invariant |

### Integration test 升級

- T12（`test_axiom_replay_round_trip`）：D12（run-scoped account_id）+ D13（polling-based wait）依本 spec section 6 升級
- 5 個 pre-existing failure：D15 加 `xfail(strict=False)` marker

### Manual verification gate（migration cutover）

每個 Migration Sequence step 完成定義已在上節 Verify column 列出。不靠 implicit 「應該沒問題」gate。

## Spec Acceptance Criteria

每項對應 Goals section 1-7 中 1 個或多個 G：

**Infrastructure**
- [ ] [G4] 3 個 Axiom dataset 存在 + 3 個 dataset-scoped API key 配置（prod / shadow / ci）
- [ ] [G4] CI dataset retention 設 3-7d（不是預設 30d）

**Source code（EventResource + envelope）**
- [ ] [G5][G6] `EventResource` + `DeploymentEnvironment` StrEnum + `_resolve_service_version` + `_resolve_host_name` + `to_otel_resource()` + `schema_version=1` 實作，unit test 100% pass + mypy strict clean
- [ ] [G5] `AxiomConfig` 加 `deployment_env` field（Shape A 或 B），unit test 100% pass + 缺 env fail-loud
- [ ] [G1][G5] `AxiomClient.resource` injection + `AxiomReplayQueryAdapter.deployment_environment` filter 實作，unit test 100% pass
- [ ] [G1] `daemon.py build_daemon` 兩端對稱 wire（emit env == query filter env），unit test 鎖契約
- [ ] [G6] envelope 含 `schema_version: 1` field，unit test 鎖 + T12 assert

**Tests**
- [ ] [G1][G2][G3] T12 升級 polling-based + run-scoped account_id + deployment_environment + schema_version assertion，CI 第一次 PR push 跑通在 90s 內
- [ ] 5 個 pre-existing integration failure 加 `xfail(strict=False)` marker + reason

**CI**
- [ ] [G7] `backend_py_unit` + `backend_py_integration` jobs 加入 `.github/workflows/ci.yml` + concurrency / needs / fork PR skip 對齊
- [ ] [G7] Workflow + per-job `permissions: contents: read` + `timeout-minutes: 10`
- [ ] [G7] `.github/dependabot.yml` 加入 github-actions ecosystem
- [ ] GitHub Actions repo secrets 配置完成（`AXIOM_CI_API_KEY` + `AXIOM_CI_DATASET`）

**Build / deploy**
- [ ] Dockerfile `ARG GIT_SHA` + `ENV BFX_SERVICE_VERSION` 加好，Koyeb build 命令 propagate
- [ ] [G4] Koyeb shadow service env 切到 shadow dataset + `BFX_DEPLOYMENT_ENV=shadow`，paper run 連續 24h healthy 驗證

**Docs**
- [ ] 本機 dev `.env` runbook + `backend_py/docs/runbooks/` 更新（含三 env 的 `.env` 範本）
- [ ] ADR 寫好（在 second-brain repo `wiki/projects/bfx-funding-bot/decisions/`，檔名格式 `YYYY-MM-DD-observability-env-separation.md`，日期 = 本 spec ship 那天；ship 後跑 `/project-decision-log` workflow compress 本 spec 進 ADR）
- [ ] Phase 4.4 cutover prework checklist 加項：(a) `backend_py_integration` required check 升級、(b) 確認 prod dataset 過 30d 已純淨

**Post-ship verification (G1/G4 measurable)**
- [ ] [G1] Ship 後 30d 內，AxiomReplayQueryAdapter 相關 PR 至少 1 次 catch wire-level issue 在 PR 階段（或 30d 無相關 PR 也可，spec attendant note）
- [ ] [G4] Phase 4.4 canary 啟動當日，Axiom prod dataset query `where deployment_environment != "prod"` 回 0 rows

## Non-goals（防 scope creep）

- ❌ Pydantic `OrderFillPayload` / `ReservationClaimedPayload` / `ReservationReleasedPayload` 加 `extra='ignore'` → **Pending list 第 2 個 separate follow-up，本 spec ship 後立刻接**
- ❌ 5 個 pre-existing integration failure 修（`test_daemon_shutdown` × 3 + `test_path_e_subprocess` + `test_warmup_determinism`）→ **本 spec 用 `xfail(strict=False)` 標記，修是 separate yak shave**
- ❌ Full OpenTelemetry SDK 採用（Pending #4 L3 upgrade，~2-3 day）→ **本 spec 的 `EventResource` 是 OTEL-inspired pre-step，未來 OTEL migration 1-1 mapping**
- ❌ Datadog / Honeycomb 整合 → **本 spec 只動 Axiom datasets**
- ❌ `service.instance.id` multi-instance support → **bfx-bot 目前 single instance per service，未來 canary/control 平行跑時加**
- ❌ Backfill pre-cutover events 標記 `deployment_environment` → **靠 Axiom 30d retention 自動清，期間 query 注意「pre-cutover events 沒有這個 field」**
- ❌ Axiom dataset retention 自動化管理（Terraform / IaC）→ **手動 Axiom console 設一次完事，IaC 是未來分項**
- ❌ T12 以外的 integration test 新加 → **本 spec 只 enable infrastructure，新增 test 是 PR-by-PR follow-up**
- ❌ CI 內跑 `pytest -m live`（real Bitfinex credentials）→ **`live` marker 既有定義 "never runs in CI"，本 spec 不動該邊界**
- ❌ **Envelope schema version-aware parser dispatch（D18 deferred logic）** → 本 spec 只加 `schema_version=1` field，未來真改 schema 時才寫 dispatch（避免 YAGNI），那時新 ADR 記 envelope schema change checklist
- ❌ GitHub Environments scope（vs repo-level secrets） → CI dataset 風險低，repo-level 足夠；未來若 secret rotation 涉及多人 approval 再升級
- ❌ Axiom OIDC token federation → Axiom 目前不支援，Open Question OQ4 追蹤
- ❌ OpenSSF Scorecard 全綠 → 本 spec 只動 `Pinned-Dependencies` + `Token-Permissions` 兩項；其他項（signed releases / branch protection / fuzz testing）是未來分項

## References

### Industry best practice references
- OpenTelemetry semantic conventions — deployment environment: https://opentelemetry.io/docs/specs/semconv/resource/deployment-environment/
- OpenTelemetry semantic conventions — service: https://opentelemetry.io/docs/specs/semconv/resource/#service
- OpenTelemetry Resource SDK: https://opentelemetry.io/docs/specs/otel/resource/sdk/
- OpenSSF Scorecard checks: https://github.com/ossf/scorecard/blob/main/docs/checks.md
- GitHub Actions security hardening: https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions
- GitHub Actions concurrency: https://docs.github.com/en/actions/using-jobs/using-concurrency
- Axiom dataset-scoped tokens: https://axiom.co/docs/reference/tokens
- Axiom retention policy: https://axiom.co/docs/reference/datasets

### Industry parallel patterns
- Stripe `req_*` request ID for sandbox test isolation
- Plaid sandbox access_token per-run isolation
- Stripe webhook integration test retry-polling pattern (vs fixed sleep)
- Datadog `env` tag + per-env dataset separation
- Honeycomb `environment` field + dataset-per-env
- EventStore / Kafka schema registry / AWS EventBridge schema versioning
