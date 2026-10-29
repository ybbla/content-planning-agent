# 03 测试、实际结果与限制

## 自动化测试

执行命令：

```bash
python -m pytest -q
```

最近一次实际结果：**100 passed**。原始输出见 `examples/test-results.txt`。测试不访问真实 API，使用临时目录隔离状态。

覆盖内容：

- 正常、超预算换路、脚本复用、无可行预算/时间缺口。
- 缺输入零模型调用、临时失败显式重试、永久失败终止。
- 重复执行零模型调用、业务输入指纹冲突。
- 非法 JSON、未知动作、提前 finish、max_steps 和零步上限。
- 缺依赖、旧估算、超预算、超时间保存硬拒绝。
- 保存后模型错误、abort、上限仍保持保存事实。
- 真实配置缺失、API 密钥脱敏、不静默降级 Mock。
- 已提交 examples 的模式、状态、费用和步骤数一致性。
- 异常 abort 字段类型、保存后异常 abort、提前/错误无方案声明。
- 永久故障连续调用、保存目录异常、损坏成功记录、非法枚举类型。
- 成本—时间权衡下的单候选缺口、动态素材 ID、篡改证据负例。
- 两次 CLI 进程失败续跑、多段步数预算、历史与 API 元数据前缀。
- 检查点校验和、伪造保存、嵌套状态损坏、故障配置/模式/模型冲突及完成清理。

## 实际运行证据

| 用例 | 实际结果 | 文件 |
|---|---|---|
| Mock 正常 | completed，12/8 | `examples/mock_normal/` |
| Mock 超预算 | 12 不可行后改估 8，completed | `examples/mock_over_budget/` |
| Mock 无预算方案 | unfinished，成本缺口 2 | `examples/mock_no_feasible_budget/` |
| Mock 时间不足 | unfinished，时间缺口 1 | `examples/mock_time_insufficient/` |
| Mock 缺预算/截止时间 | 对应原因，零步骤 | `examples/mock_missing_budget/`、`examples/mock_missing_deadline/` |
| Mock 临时工具失败 | 失败后显式重试，completed，保留搜索 | `examples/mock_tool_failure/` |
| Mock 重复执行 | 第二次 duplicate=true、零步骤、费用不变 | `examples/mock_repeat_first/`、`examples/mock_repeat_second/` |
| Mock 步数上限 | unfinished/max_steps，2 步 | `examples/mock_max_steps/` |
| Mock 失败续跑 | 第一段 2 步失败停止，第二段恢复并新增 4 步，累计 6 步完成 | `examples/mock_resume_failure_first/`、`examples/mock_resume_failure_second/` |
| real 缺配置 | unfinished/model_unavailable，未切换 Mock | `examples/real_model_unavailable/` |

`python -m src.validate_examples` 已实际通过上述 Mock 证据校验。

## 真实模型验证状态

**已完成。** 使用火山方舟 OpenAI 兼容 Responses API、模型 `doubao-seed-2-1-pro-260915` 实际运行并通过：

| 真实用例 | 实际结果 | 文件 |
|---|---|---|
| 正常 | 5 步，保存 12/8 | `examples/real_normal/` |
| 超预算 | 观察 12 不可行后复用脚本改估 8，再保存，共 6 步 | `examples/real_over_budget/` |
| 一次性工具失败 | 生成脚本临时失败后，模型明确决定重试，保存 12/8，共 6 步 | `examples/real_tool_failure/` |
| 失败续跑第一段 | 真实 API 两步后在脚本故障处停止，保存检查点 | `examples/real_resume_failure_first/` |
| 失败续跑第二段 | 恢复前两步，模型显式重试并新增 4 步，累计 6 步保存 12/8 | `examples/real_resume_failure_second/` |

实际执行 `python -m src.validate_examples --require-real` 已通过 Mock 与五段 real 证据。每步 real Trace 均含唯一 `response_id`、匹配模型的 provider model 和 created 元数据，不含密钥或原始响应。续跑校验额外比较两段历史步骤、API 元数据前缀、恢复 run_id 和新增步数；人工复核确认超预算、故障恢复及续跑动作确由模型根据观察选择。

## 已知限制

- 未进行并发进程测试，首版只承诺顺序幂等。
- 失败续跑仅保证正常返回未完成后的顺序恢复；工具执行与检查点写入不是事务，强杀窗口内可能需重放非保存工具，多进程并发续跑仍需任务锁。
- 真实模型行为可能因供应商兼容性和模型而异；协议错误会消耗步数。
- pytest 输出出现来自环境 `pytest-asyncio` 的弃用提示，不影响结果，本项目未使用该插件。
