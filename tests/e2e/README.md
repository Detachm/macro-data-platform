# End-to-end tests

在这里使用 mock provider 跑通 `worker → PostgreSQL → REST API → EditorContext`，覆盖重跑、崩溃恢复、provider 降级和历史 `as_of`。
