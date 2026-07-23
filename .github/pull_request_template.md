## 关联 Issue

Closes #

## 修改目的

<!-- 说明解决的问题，不要只罗列文件。 -->

## 合同与数据影响

- 涉及区域：
- 输入示例：
- 输出示例：
- 错误与降级行为：
- 公共 contract 是否变化：否 / 是（附 ADR）
- 数据库是否变化：否 / 是（附 migration）
- 保存、授权或 LLM 传输是否变化：否 / 是（说明依据）

## 验证

- 新增测试及测试 ID：
- 本地测试命令与结果：
- 在线验证方式（如适用）：
- 回滚方式：

## 提交前检查

- [ ] PR 只处理一个 Issue，且不包含密钥、Cookie 或受限正文
- [ ] 新 provider 使用公共 contracts，没有区域私有公共 DTO
- [ ] 时间均带时区，区间遵循 `[start, end)`，PIT 过滤使用 `available_at`
- [ ] fixture 为合成或脱敏数据，并覆盖空页、限流、超时和 schema drift
- [ ] `ruff format/check`、`mypy`、`pytest`、migration 检查通过
- [ ] 数据源登记、运行手册和授权字段已同步更新
