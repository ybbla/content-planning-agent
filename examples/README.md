# 运行证据索引

每个用例目录包含实际 `input.json`、`result.json`、`trace.json`。`run_id` 用于区分同一任务的多次运行；Trace 只含简要决策依据，不含内部思维过程。

## 已生成并校验

- `mock_normal`：正常保存，12/8。
- `mock_over_budget`：12 不可行后改估 8，脚本复用。
- `mock_no_feasible_budget`：成本缺口 2，无保存。
- `mock_time_insufficient`：时间缺口 1，无保存。
- `mock_missing_budget`、`mock_missing_deadline`：缺输入，零模型动作。
- `mock_tool_failure`：一次性生成失败后显式重试，之前搜索保留。
- `mock_repeat_first`、`mock_repeat_second`：第二次 duplicate=true，零模型动作。
- `mock_max_steps`：2 步后明确未完成。
- `mock_resume_failure_first`、`mock_resume_failure_second`：故障后保存检查点，第二次恢复并新增 4 步完成。
- `real_model_unavailable`：real 缺配置时明确失败，没有自动使用 Mock。
- `test-results.txt`：最近一次完整 pytest 原始输出。

## 真实模型证据（已生成并校验）

- `real_normal`：Doubao Responses API 正常保存，12/8，5 步。
- `real_over_budget`：观察 12 不可行后复用脚本改估 8，再保存，6 步。
- `real_tool_failure`：临时生成失败后由模型显式重试，保存 12/8，6 步。
- `real_resume_failure_first`：真实模型两步后在临时脚本故障处停止并保存检查点。
- `real_resume_failure_second`：恢复前两步及 API 元数据，模型显式重试并新增 4 步完成。

五段记录均包含逐步 API `response_id`、provider model 和 created 元数据。续跑两段还核对了历史前缀、恢复 run_id 和新增步数。`python -m src.validate_examples --require-real` 已实际通过。不得复制 Mock 文件或手工修改模型动作来冒充 real 证据。
