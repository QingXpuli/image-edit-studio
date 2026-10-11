# AI-first image workflow

本工作区的图像任务默认由 AI 解析自然语言并调用 CLI 完成；网页只作为遮罩绘制或画廊辅助入口，DSH 不属于默认运行路径。

## 路由契约

先将需求归类为以下一种：

- `deterministic_local`：调淡、放大、裁切、拼版、调子报告等确定性像素处理；走 `zimage.py local`，不调用上游。
- `localized_edit`：局部替换、修手、换背景、重画道具；走 `zimage.py edit` 的 mask 路径。
- `whole_style_transfer`：整图风格或色调变化；由 `style-distill` 编译提示词，再走 `zimage.py edit --whole`。
- `pose_transfer`：参考图提供动作/构图，目标图提供主人公身份；先做图角色分析和提示词编译，再走整图或局部执行。
- `character_preservation`：明确锁定目标人物身份；提示词与验收必须列出身份保持项和参考身份泄漏项。
- `prompt_only`：只需要提示词、分析或方案，不调用上游。
- `inspect` / `gallery`：只读检查或浏览成果，不调用上游。

结构化任务至少记录：`intent`、`input_roles`、`change_scope`、`deliverable`、`side_effect_policy`、`ui_policy`。

## 默认不变量

1. 先判断是否能用确定性本地操作解决；能解决就不生成。
2. 生成先查 fingerprint/cache，再做本地 plan/dry-run；真实提交只能由主流程唯一 owner 发起。
3. 子代理只能做输入角色分析、提示词审查或结果验收，不得提交、重试、创建第二个 fingerprint、修改技能或经验规则。
4. `prompt_only`、inspect、gallery、dry-run 不 POST；受理状态未知时停止，不自动重试。
5. 结果不能仅凭 HTTP 200、文件存在或可解码判定成功；必须按任务要求做技术和语义验收。
6. 身份源明确；不默认 `input_fidelity=low` 保身份。内容、配色和参考迁移按用户 scope；WD14、Lineart、调色板、JoyCaption、三张卡、姿态/多视图检查按需启用。
7. 网页有三处：涂遮罩（`/`）、画廊（`/gallery`）、无限画布（`/board`）。它们仍回 CLI generation；网页不是第二个发送器。DSH 仅作历史/兼容参考。
8. 用户认可版本是基准；单变量只用于诊断，连续两轮失败就停。经验先做候选，人工审核后才写 references，不自动循环写 skill。
9. 结论标注【实测】、【文件核实】、【判断】或【未验证】；真实失败不得伪装成成功。尺寸阈值、retry 502、b64 下述、物理硬贴和当前 Grok 恢复/fallback 都不得泛化。

## 任务与 review 契约

未来 CLI 的 `--task-file` 计划使用 `zimage-task-v1`：必填 `intent`、`input_roles`、`change_scope`、`deliverable.path`、`deliverable.size`、`side_effect_policy.generation_authorized`、`ui_policy`、`required_checks`。计划中的 `review --job` 使用 `--verdict pass|fail|uncertain` 和 `--check x=pass` 等逐项结果；当前实现以实际 CLI 为准，不能把此契约当成已完成能力。详见 `docs/current-policy.md`。

## 询问边界

信息充分且任务已授权时自动推进。仅在身份图/参考图角色无法判断、输出比例或成本取舍会改变结果、要求互相冲突、或验收标准无法推断时询问用户。
