# 内容任务规划 Agent

面向运营人员的内容活动方案规划 CLI。程序通过四个本地业务工具完成素材检索、脚本生成、成本估算和方案保存；真实模型逐轮选择动作并观察结果，在超预算或工具失败后重规划、显式重试或终止。

> 当前验证状态：100 项离线 pytest 通过，12 组 Mock 运行证据及 5 组真实 Doubao Responses API 运行证据（含两段失败续跑）均通过 `python -m src.validate_examples --require-real` 严格校验。

## 环境与安装

- Python 3.12（本机验证为 3.12.3）
- 依赖版本见 `requirements.txt`

```bash
python -m pip install -r requirements.txt
```

## 真实模型配置

复制 `.env.example` 为本地 `.env`，填入 OpenAI 兼容接口配置：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://你的兼容接口/v1
OPENAI_MODEL=你的模型名
# 火山方舟 Responses API 使用 responses；其他兼容服务可默认 chat。
OPENAI_API_STYLE=responses
```

不要提交 `.env`。默认 `--mode real`；真实模型不可用会返回 `model_unavailable`，不会自动切换 Mock。

## 运行

在仓库根目录执行：

```bash
# 真实模型：正常、超预算换路、一次性工具失败
python -m src.main --scenario data/scenarios/normal.json --mode real --state-dir data/runtime/real --output-dir examples/real_normal
python -m src.main --scenario data/scenarios/over_budget.json --mode real --state-dir data/runtime/real --output-dir examples/real_over_budget
python -m src.main --scenario data/scenarios/tool_failure.json --mode real --state-dir data/runtime/real --output-dir examples/real_tool_failure

# 真实失败续跑（两段共用同一 state-dir）
python -m src.main --scenario data/scenarios/resume_failure.json --mode real --max-steps 2 --state-dir data/runtime/real-resume --output-dir examples/real_resume_failure_first
python -m src.main --scenario data/scenarios/resume_failure.json --mode real --state-dir data/runtime/real-resume --output-dir examples/real_resume_failure_second

# 显式离线 Mock
python -m src.main --scenario data/scenarios/over_budget.json --mode mock --state-dir data/runtime/mock --output-dir examples/mock_over_budget

# 重复执行：第二次 duplicate=true，模型与工具零调用
python -m src.main --scenario data/scenarios/repeat.json --mode mock --state-dir data/runtime/repeat --output-dir examples/mock_repeat_first
python -m src.main --scenario data/scenarios/repeat.json --mode mock --state-dir data/runtime/repeat --output-dir examples/mock_repeat_second

# 步数上限
python -m src.main --scenario data/scenarios/normal.json --mode mock --max-steps 2 --state-dir data/runtime/max --output-dir examples/mock_max_steps

# 失败续跑：第一次故障后达到上限并保存检查点，第二次从历史状态继续
python -m src.main --scenario data/scenarios/resume_failure.json --mode mock --max-steps 2 --state-dir data/runtime/resume --output-dir examples/mock_resume_failure_first
python -m src.main --scenario data/scenarios/resume_failure.json --mode mock --state-dir data/runtime/resume --output-dir examples/mock_resume_failure_second
```

每次输出 `input.json`、`result.json`、`trace.json`。续跑结果用 `resumed`、`resume_count`、`resumed_from_run_id` 标记来源，`step_count` 为累计步数，`new_step_count` 为本次新增步数。退出码：完成（含重复）为 0，业务/输入/模型未完成为 2，未预期程序错误为 1。Windows 终端若中文乱码，可设置 `PYTHONIOENCODING=utf-8`；JSON 文件始终为 UTF-8。

## 测试与证据校验

```bash
python -m pytest -q
python -m src.validate_examples
# 强制检查全部 Mock、真实模型及两段续跑交付证据：
python -m src.validate_examples --require-real
```

实际 pytest 输出见 `examples/test-results.txt`，评估详情见 `docs/03-evaluation.md`。

## 评审入口

| 验收点 | 证据 |
|---|---|
| 正常完成 | `examples/mock_normal/`、`examples/real_normal/` |
| 预算 10：12 改选 8 | `examples/mock_over_budget/`、`examples/real_over_budget/` |
| 工具失败、成功结果保留 | `examples/mock_tool_failure/`、`examples/real_tool_failure/` |
| 缺预算/截止时间 | `examples/mock_missing_budget/`、`examples/mock_missing_deadline/` |
| 无可行方案及缺口 | `examples/mock_no_feasible_budget/`、`examples/mock_time_insufficient/` |
| 重复执行 | `examples/mock_repeat_first/`、`examples/mock_repeat_second/` |
| 步数上限 | `examples/mock_max_steps/` |
| 失败步骤续跑 | `examples/mock_resume_failure_first/`、`examples/mock_resume_failure_second/`；真实证据见 real 同名目录 |
| 真实模型不可用、不降级 | `examples/real_model_unavailable/` |

## 已知边界

- 费用是最终选中方案的模拟估算，不是工具调用扣款流水；被替代方案不累加。
- 支持正常返回未完成后的跨进程顺序续跑；每次动作后保存检查点，完成后清理。不含并发锁、强杀事务恢复或生产级 Runtime。
- Mock 只验证流程，不证明真实模型决策能力。
- 可测量实现、加固、真实模型接入与失败续跑用时为 3 小时 27 分，计时口径见 `docs/05-retrospective.md`。

## 提交检查

- 题号：内容任务规划 Agent
- GitHub URL：https://github.com/ybbla/content-planning-agent
- 最终 commit SHA：以远程 `main` 最新提交为准，推送后在交付回复中给出。
- 发布前再次运行 `python -m src.validate_examples --require-real`；不要提交密钥或 `data/runtime/`。
