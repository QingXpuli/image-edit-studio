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
2. 生成默认先做本地 plan/dry-run；真实提交只能由主 AI 流程统一发起。
3. 子代理只能做输入角色分析、提示词审查或结果验收，不得提交、重试、创建第二个 fingerprint、修改技能或经验规则。
4. 相同请求必须先查 fingerprint cache；真实提交必须有唯一 owner，不能并行重复扣费。
5. 结果不能仅凭 HTTP 200、文件存在或可解码判定成功；必须按任务要求做技术和语义验收。
6. 网页有三处：涂遮罩（`/`）、画廊（`/gallery`）、无限画布（`/board`，即梦式）。画布可放图、加字、写提示词并触发生成，但仍走 `zimage.py edit` / `run_round.py`：一次 POST、指纹缓存、环境变量凭据、完整解码。聊天里的 AI 仍负责分类与提示词编译；画布不是第二个发送器。手涂复杂遮罩完成后仍可回到 CLI 的 mask-file 流程。
7. DSH 仅作历史/兼容参考，不作为 ZCode 图像流程依赖。
8. 结论标注【实测】、【文件核实】、【判断】或【未验证】；真实失败不得伪装成成功。

## 询问边界

信息充分且任务已授权时自动推进。仅在身份图/参考图角色无法判断、输出比例或成本取舍会改变结果、要求互相冲突、或验收标准无法推断时询问用户。
