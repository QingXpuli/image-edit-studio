---
name: style-distill
description: 图像语义层：在不生成的前提下分析风格、姿态、多视图和角色身份，编译可审阅的提示词；用于风格迁移、姿态迁移、参考图分析、失败归因和 scope 明确。
---

# style-distill 语义层

本技能只观察、分类、编译和验收，不直接提交 API。真实执行交给 `image-edit`。用户只要提示词、分析或方案时，输出 prompt/报告并保持 `prompt_only`，不 POST。

## 开工分类

1. 能由现有像素确定完成（调淡、放大、裁切、拼版、调色、去砂）？标记 `deterministic_local`，交给本地处理。
2. 要重画局部？标记 `localized_edit`，明确区域和是否允许抹掉原信息。
3. 要整图风格、色调、姿态或多视图？继续做语义分析，再交 `whole_edit`。
4. 只要分析/提示词/浏览？分别标记 `prompt_only` / `inspect` / `gallery`，不发送。

记录 `intent`、`input_roles`、`change_scope`、`deliverable`、`side_effect_policy`、`ui_policy`。身份源、风格源、姿态源各自明确；不确定就问，不把参考图角色、服饰、道具或配色泄漏进目标。

## 最小观察规则

- 单图或 1–2 张：只做轻量观察。不要套完整模板，不强制 WD14、Lineart、调色板、JoyCaption 或三张卡全套。
- 同风格组（通常 >=6 张）且用户确实要统计：才读取 `references/template.md`，按需做分组、反例和阈值。
- 姿态任务：只读取姿态/几何锚点相关 references；以角色自身方位描述头、肩、手、关节、遮挡和厚度。
- 多视图任务：逐图标注来源、视角、身份保持和跨图一致项；不把多视图拼成一张来代替多图字段。
- 风格任务：观察 LINE、EDGE、FILL、VALUE、HAIR、FACE、FABRIC、GROUND、FINISH 中真正看见的项；看不出就写未识别。FACE、配色和身份不是全局默认。

按需读取：失败词用 `references/negative-library.md`，身份泄漏用 `references/identity-library.md`，接口/尺寸/遮罩事实用 `references/pipeline-notes.md` 或 image-edit 的 `references/execution.md`。不要为只读分析调用上游。

## 提示词编译

先写角色表，再写可审阅草稿：

`HEAD → PRESERVE → (POSE/VIEW 按需) → COLOUR SCOPE → STYLE（仅观察事实） → DO NOT INCLUDE → 验收项`

- 目标图是身份和颜色来源；参考图只提供用户授权的手法、姿态或视图。
- 用户明确要保身份时，列身份保持项和参考泄漏项；不默认 `input_fidelity=low`。只有用户明确允许内容/身份重构时才建议低保真路线。
- 配色迁移只在用户明确要求时开启；否则保留目标图配色。单变量用于诊断，不把“每轮一个变量”升级成每次任务强制多轮。
- 负向只写可观察失败态或参考泄漏，不用空泛的“不要崩”；目标图已有的大元素先物理处理，不能靠负向删除。
- 基准版本一经用户认可就冻结。后续按“基准 + 用户点名的单一变化”修正；不得为了追风格从零重掷。

## 验收与升级

技术指标由执行层提供；语义验收逐项描述实际画面，并区分人工判断与脚本数据。核对身份、用户点名的变化、未授权泄漏、构图/遮挡和邻区连带。HTTP 200、文件存在、尺寸正确或标签重合率都不能单独证明语义成功。

同一失败连续两轮：停止改词，报告已见证据，选择局部重绘、改用确定性处理、换经确认的通道或回滚基准；不自动重试，不假设 502 可恢复，也不把当前 Grok 当默认 fallback。复杂任务可让只读子代理审查，但只有主流程能提交。

## 经验边界

LESSONS 是历史索引，不是默认规则。新经验先作为候选并人工审核，审核后才更新 references；不会自动循环写 skill。完整旧账本在 `references/history-lessons-repository.md` 与 `references/history-lessons-runtime-2026-10-10.md`，旧 SKILL 在 `references/history-style-distill-skill.md`，需要追溯时才读。

所有结论标注 `【实测】`、`【文件核实】`、`【判断】` 或 `【未验证】`。不写真实 key；不把特定 relay 的尺寸阈值、速度、b64 返回尺寸或物理硬贴效果泛化。
