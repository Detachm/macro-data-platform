# Live smoke tests

在线测试必须标记 `@pytest.mark.live`，只校验合同、不变量和合理范围。默认 CI 禁止执行；凭据来自 Secret Manager，不得写入 fixture 或日志。
