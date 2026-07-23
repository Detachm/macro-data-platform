# Provider fixtures

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
```

fixture 只能使用合成或脱敏数据。测试内固定时钟、request ID 和 as-of；不得包含 token、Cookie、个人信息或无权保存的新闻正文。字段解释和授权依据写入 `docs/data-sources/`，不得只写在测试代码里。
