---
name: image-edit
description: ZCode 本地图像执行层：在语义任务已明确后，按审批闸门运行 zimage.py 或执行确定性本地处理；用于换背景、修局部、放大、裁切、拼版、调子报告和画廊。
when_to_use: 用户要执行已有任务规格、修改已有图片、做确定性像素处理或查看画廊时使用；语义风格/姿态分析交给 style-distill。
---

# image-edit 执行层

本技能只负责执行和技术验收。语义分析、身份/姿态/风格观察由主代理完成，必要时读取 `style-distill`；子代理只能只读审查。DSH 不是默认路径。

## 先路由

- `deterministic_local`：调淡、放大、裁切、拼版、调子报告、去砂等，走 `zimage.py local`，不 POST。
- `localized_edit`：明确要重画一块，走 `edit` 的区域或 mask 路径。
- `whole_edit`：已完成语义编译后才走 `edit --whole`。
- `prompt_only`、`inspect`、`gallery`：只分析、检查或浏览，不 POST。

结构化记录至少包括 `intent`、`input_roles`、`change_scope`、`deliverable`、`side_effect_policy`、`ui_policy`。不清楚身份图、参考图、比例或成本时先问；信息充分且已授权才推进。

## 执行闸门

1. 先查 fingerprint/cache；同一请求只能有一个真实提交 owner。
2. 首次配置或排障时运行 `doctor`；正常任务直接生成本地 plan/dry-run。dry-run 不需要凭据，也不发送。
3. 发送前核对角色表、区域、比例、提示词、遮罩和预算；局部任务先检查 redmark 落点。
4. 真实提交必须是一次 POST；受理状态未知时停止，不自动重试。`prompt_only` 永远不 POST。
5. 结果须完整解码、核对实际尺寸/字节，并按任务做语义验收；HTTP 200、文件存在或可解码都不单独算成功。
6. 身份保持必须列出身份源、保持项和参考身份泄漏项。不要默认 `low`；只有用户授权不保身份的画风迁移才考虑它。
7. 保留用户认可的基准版本；单变量只用于诊断，失败两轮停止改词并升级路线，不从零重掷基准。
8. 经验只登记为候选，须人工审核后才进入 references；不自动循环写 SKILL 或词库。

## 常用入口

从工作区根目录运行 `C:\Python314\python.exe zcode-image-edit\bin\zimage.py`：

- `doctor`：依赖、凭据和脚本自检，不调接口。
- `plan --plan-out PATH`：保存预检、哈希、指纹和预算，不发送。
- `edit`：区域参数 `--rect`、`--polygon`、`--flood`、`--grabcut`、`--mask-file`；整图用 `--whole`。
- `local`：确定性像素处理，不调接口。
- `serve`、`board`、`gallery`、`stop`：网页仅作遮罩、画布或浏览辅助，生成仍回 CLI。

## 局部语义

默认“涂哪改哪”使用 `--mask-primary`；它不是逐像素保护，整图仍可能轻微变化。`--protect`/反向 mask 表示“涂哪保哪”，只是模型约束，必须用保护区 metrics 和语义 review 验证，不能当作确定性像素保护或整体调色方案。硬涂会抹掉区域原信息，只适合本来就要清掉重画的区域。详细技术口径见 `references/execution.md`。

## 纪律

凭据只从 `RELAY_API_KEY`、`RELAY_BASE_URL`、`RELAY_MODEL` 环境变量读取，不写入文件。命令和结论标注依据：`【实测】`、`【文件核实】`、`【判断】`、`【未验证】`。尺寸阈值、速度、b64 行为和模型通道都是特定 relay 的观测，不当作普世保证；retry 502、b64 下述、硬贴效果及当前 Grok 恢复/fallback 需按当前环境重新验证。
