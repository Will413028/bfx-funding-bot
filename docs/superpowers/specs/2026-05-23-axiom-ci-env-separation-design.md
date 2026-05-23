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
  "deployment_environment": "ci",                  # NEW (OTEL: deployment.environment.name)
  "service_name": "bfx-funding-bot",               # NEW (OTEL: service.name)
  "service_version": "0447e29a",                   # NEW (OTEL: service.version, $GIT_SHA)
  "payload": { ...unchanged... },                  # existing
}
```

Payload schemas（`OrderFillPayload` / `ReservationClaimedPayload` / `ReservationReleasedPayload`）**完全不動**。

## Source Code Changes

### 1. New module: `src/bfx_funding_bot/modules/observability/resource.py`

```python
from dataclasses import dataclass, field
from enum import StrEnum

class DeploymentEnvironment(StrEnum):
    PROD = "prod"
    SHADOW = "shadow"
    CI = "ci"

def _resolve_service_version() -> str:
    """Resolve service version from build-time env or git fallback.

    Build: Dockerfile ARG GIT_SHA → ENV BFX_SERVICE_VERSION
    Local dev: git rev-parse HEAD fallback
    """
    import os, subprocess
    v = os.environ.get("BFX_SERVICE_VERSION")
    if v:
        return v
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"

@dataclass(frozen=True)
class EventResource:
    """Process-level immutable attributes attached to every event.

    Aligned with OpenTelemetry Resource semantic conventions:
    - deployment.environment.name → deployment_environment
    - service.name → service_name
    - service.version → service_version
    """
    deployment_environment: DeploymentEnvironment
    service_name: str = "bfx-funding-bot"
    service_version: str = field(default_factory=_resolve_service_version)

    def envelope_fields(self) -> dict[str, str]:
        return {
            "deployment_environment": self.deployment_environment.value,
            "service_name": self.service_name,
            "service_version": self.service_version,
        }
```

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
    assert all(r["deployment_environment"] == "ci" for r in rows)  # NEW
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

完整 `.github/workflows/ci.yml` 增量（既有 `backend` / `frontend` job 不動）：

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  # existing: backend (Go, archived), frontend (Next.js) — unchanged

  backend_py_unit:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: backend_py
    steps:
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
| `tests/unit/observability/test_event_resource.py`（new） | `EventResource` immutability（frozen=True 違反 raise）/ `envelope_fields()` 鍵名對齊 OTEL semantic conventions / `DeploymentEnvironment` enum exhaustive / `_resolve_service_version` build-env 優先 / git fallback / unknown fallback |
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

- [ ] 3 個 Axiom dataset 存在 + 3 個 dataset-scoped API key 配置
- [ ] `EventResource` + `DeploymentEnvironment` StrEnum + `_resolve_service_version` 實作，unit test 100% pass + mypy strict clean
- [ ] `AxiomConfig` 加 `deployment_env` field（或新 settings class），unit test 100% pass
- [ ] `AxiomClient.resource` injection + `AxiomReplayQueryAdapter.deployment_environment` filter 實作，unit test 100% pass
- [ ] `daemon.py build_daemon` 兩端對稱 wire（emit env == query filter env），unit test 鎖契約
- [ ] T12 升級 polling-based + run-scoped account_id + deployment_environment filter，CI 第一次 PR push 跑通
- [ ] 5 個 pre-existing integration failure 加 `xfail(strict=False)` marker + reason
- [ ] CI workflow `backend_py_unit` + `backend_py_integration` jobs 加入 `.github/workflows/ci.yml` + concurrency / needs / fork PR skip 對齊
- [ ] GitHub Actions repo secrets 配置完成（`AXIOM_CI_API_KEY` + `AXIOM_CI_DATASET`）
- [ ] Dockerfile `ARG GIT_SHA` + `ENV BFX_SERVICE_VERSION` 加好，Koyeb build 命令 propagate
- [ ] Koyeb shadow service env 切到 shadow dataset，paper run 連續 24h healthy 驗證
- [ ] 本機 dev `.env` runbook + `backend_py/docs/runbooks/` 更新
- [ ] ADR 寫好（在 second-brain repo `wiki/projects/bfx-funding-bot/decisions/`，檔名格式 `YYYY-MM-DD-observability-env-separation.md`，日期 = 本 spec ship 那天；ship 後跑 `/project-decision-log` workflow compress 本 spec 進 ADR）
- [ ] Phase 4.4 cutover prework checklist 加 `backend_py_integration` required check 升級項

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

## References

- OpenTelemetry semantic conventions: https://opentelemetry.io/docs/specs/semconv/resource/deployment-environment/
- Axiom dataset-scoped tokens: https://axiom.co/docs/reference/tokens
- GitHub Actions concurrency: https://docs.github.com/en/actions/using-jobs/using-concurrency
- Industry parallel — Stripe webhook integration test pattern：retry polling vs fixed sleep
