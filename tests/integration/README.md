# Integration tests

在这里使用临时 PostgreSQL 验证 migration、约束、Decimal/UTC 往返、幂等 upsert、watermark 与 PIT 查询。测试不得访问外部 provider。
