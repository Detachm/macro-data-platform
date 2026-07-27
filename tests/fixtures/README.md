# Provider fixtures

默认测试不得访问公网、不得读取真实凭据。`tests/fixtures/cn/manifest.json` 和
`tests/fixtures/hk/manifest.json` 是 Issue #8 的结构入口，只登记 synthetic/fixture
provider、registry role 和测试矩阵，不代表 CN/HK 真实 provider 已通过验收。

复现结构：

```bash
uv run pytest tests/contract/test_cn_hk_providers.py -q
```

如果本机没有 `uv`，使用项目要求的 Python 3.12 环境执行同一个 pytest 入口。

每个真实 provider 建立独立子目录，例如 `cn/provider_name/`，至少包含：

```text
success.json
empty.json
missing_fields.json
auth_failure.json
rate_limited.json
timeout.json
schema_changed.json
duplicate_page.json
html_login.json
headline_only.json
```

fixture 只能使用合成或脱敏数据。测试内固定时钟、request ID 和 as-of；不得包含 token、Cookie、个人信息或无权保存的新闻正文。字段解释和授权依据写入 `docs/data-sources/`，不得只写在测试代码里。

## CN/HK contract fixture manifests

Issue #8 reserves CN/HK provider contract structure without claiming real providers pass.

- CN manifest: `tests/fixtures/cn/manifest.json`
- HK manifest: `tests/fixtures/hk/manifest.json`
- Shared assertions: `tests/contract/provider_suite.py`
- Regional entrypoints: `tests/contract/test_cn_hk_providers.py`

Default contract tests are offline and require no real credentials:

```bash
uv run pytest tests/contract/test_cn_hk_providers.py -ra
```

The synthetic providers use only `tests/fixtures/{cn,hk}/synthetic/*`. They are fake fixture
providers, not live CN/HK adapters. Registry roles are declared for structure-first loading only;
fixture-only datasets must be rejected by `assert_production_dataset_supported`; live-ready
datasets follow the frozen capability matrix and must not be scheduled as live providers.

Only fixture-protocol cases remain `xfail`; each has a concrete follow-up Issue #21. Their blocker
and deferred reason are mirrored in `CONTRACT_CASES` and both regional manifests.

| Case | Blocker | Deferred reason |
| --- | --- | --- |
| PRV-003 | #21 | requires fixture pagination protocol |
| PRV-004 | #21 | requires boundary-record fixture |
| PRV-005 | #21 | requires unordered upstream page fixture |
| PRV-006 | #21 | requires fixture quarantine behaviour |
| PRV-015 | #21 | requires fixture cursor continuation protocol |

1. Add the real provider fixture set under `tests/fixtures/{cn,hk}/<provider_name>/`.
2. Keep assertions in `tests/contract/provider_suite.py`; add only provider parameters or fixtures.
3. Change each manifest case from `xfail` to `implemented` only when the real provider/fixture path
   exercises that case.
4. Remove the matching `pytest.xfail(...)` entry from `CONTRACT_CASES`.
5. Update the PR description with implemented IDs and remaining blocked IDs.

PR description seed:

```text
Implemented test IDs:
- PRV-001, PRV-002, PRV-007, PRV-008, PRV-009, PRV-010, PRV-011, PRV-012,
  PRV-013, PRV-014, PRV-016, PRV-017, PRV-018, PRV-019, PRV-020
- NEWS-002, NEWS-003, NEWS-012, NEWS-013, NEWS-017
- PIT-AVAILABLE-AT-AS-OF

Still blocked:
- PRV-003, PRV-004, PRV-005, PRV-006, PRV-015

Blocked by: #21 as recorded in tests/fixtures/{cn,hk}/manifest.json.
```

## CN/HK contract matrix

公共 suite 位于 `tests/contract/provider_suite.py`。CN 与 HK 的入口只在
`tests/contract/test_cn_hk_providers.py` 参数化 fixture provider，不复制区域私有断言。

已落地 fixture 入口：

- `PRV-001`、`PRV-002`、`PRV-013`、`PIT-AVAILABLE-AT-AS-OF`：success fixture，
  覆盖 provenance、checksum、query immutability 和 `available_at <= as_of`。
- `PRV-007`、`PRV-008`、`PRV-009`、`PRV-010`、`PRV-019`、`PRV-020`：
  错误 fixture，覆盖 429、auth、schema drift、timeout、重复页/游标、HTML 登录页和未知字段
  不被当成空数据。
- `NEWS-002`、`NEWS-003`、`PRV-017`：URL canonicalization、标题 normalization 和
  canonical checksum。
- `NEWS-012`、`NEWS-013`、`NEWS-017`：title-only 新闻、空 vendor annotation，并保留
  legacy rights 字段骨架；内部个人运行时不读取 rights 作准入判断，summary/body 保持原样。

已实现的 `PRV-011`、`PRV-012`、`PRV-014`、`PRV-016` 由 #20 的真实 PostgreSQL
handler 回归覆盖：分别验证外层回滚后 PIT 审计仍保留、原始时区审计、并发 reservation/
重试幂等，以及 cursor 过期后从 committed watermark 恢复且不重复写入。

保留为 `xfail` 的入口仅为：`PRV-003`、`PRV-004`、`PRV-005`、`PRV-006`、`PRV-015`；
每项的原因均写在测试和 manifest 中，统一由 #21（fixture-provider contract）跟踪。

## 完成 #20 / #21 后解除 xfail

1. 在 `tests/fixtures/{cn,hk}/<real_provider>/` 增加真实 provider 的脱敏 fixture，并在
   manifest 中登记真实 provider ID。
2. 将 manifest 里对应测试 ID 从 `xfail` 改为 `implemented`，填入 fixture 名称。
3. 在 `tests/contract/test_cn_hk_providers.py` 的参数表加入真实 provider fixture entrypoint。
4. 只在 live 测试中使用真实公网和凭据，并加 `@pytest.mark.live`；默认 contract 测试仍必须离线。
