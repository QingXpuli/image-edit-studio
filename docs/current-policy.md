# 当前 policy

本文是当前默认口径的短入口。旧迁移文档保留历史 warning；细节按需读取 `zcode-image-edit/_skill/image-edit/references/execution.md` 与 `style-distill/_skill/style-distill/references/`。

## 路由

- `deterministic_local`：本地确定性处理，不生成。
- `localized_edit`：局部重画，需明确区域、是否允许抹掉原信息和 mask 语义。
- `whole_style_transfer` / `pose_transfer`：先做语义角色表和 scope，再交 image-edit 执行。
- `prompt_only`、inspect、gallery：只读，不 POST。

每个任务记录 `intent`、`input_roles`、`change_scope`、`deliverable`（path/size）、`side_effect_policy`、`ui_policy`；生成前还要有 `generation_authorized`、fingerprint/cache、owner、required checks。当前执行仍以现有 CLI 为准。

## 默认闸门

先查缓存，再按需 plan/dry-run、redmark 和预算；`doctor` 仅用于首次配置或排障。真实生成只能一次 POST，由主流程唯一 owner 发起。结果须完整解码、实际尺寸核对和语义 review；不能只凭 HTTP 200、文件存在或可解码放行。认可版本冻结，单变量只用于诊断，连续两轮失败停下，不自动重试或默认 fallback。

身份源明确；不默认 low 保身份。配色和内容迁移按用户 scope；WD14、Lineart、调色板、JoyCaption、三张卡、姿态/多视图检查按需启用。结论标注 `【实测】`、`【文件核实】`、`【判断】`、`【未验证】`。

## 待实现 CLI 契约

CLI 计划支持 `--task-file`，schema 名 `zimage-task-v1`，必填：`intent`、`input_roles`、`change_scope`、`deliverable.path`、`deliverable.size`、`side_effect_policy.generation_authorized`、`ui_policy`、`required_checks`。计划中的 `review --job` 支持 `--verdict pass|fail|uncertain` 与 `--check x=pass` 等逐项检查。app APIs 的生成入口统一归 CLI generation（含 `generate`）；此处是待实现契约，不声称当前命令已经全部支持。

## 安装与历史边界

本次只改仓库文档/技能和复制的 history；runtime 安装副本未更新。global workflow 仅作历史背景，未禁用或修改。未执行 archive、移动或删除；legacy direct scripts、旧插件路径和任何“完全单一 sender/video”说法均不因本文消失。插件安装与 marketplace 变更未授权。
