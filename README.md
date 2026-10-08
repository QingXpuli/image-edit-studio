# 图生图工具站（Image Edit Studio）

AI 主导的图生图 / 局部改图 / 风格蒸馏工作台：**自然语言下指令，AI 完成任务分类、提示词编译、预检、提交、结果验收与经验沉淀**。本项目在 ZCode（或任何带终端工具的 Agent 环境）中原生运行，**不依赖 DSH**；网页只作为手涂遮罩与画廊的辅助入口。

> 仓库纪律：所有结论标注【实测】/【文件核实】/【判断】/【未验证】；能出数的判断不靠肉眼；凭据只走环境变量，不落盘、不进仓库。

## 核心能力

| 能力 | 入口 | 说明 |
|---|---|---|
| 整图风格迁移 / 姿态迁移 | `zimage.py edit --whole` | 目标图提供身份，参考图提供动作/构图/画风 |
| 局部改图（涂哪改哪） | `zimage.py edit --rect/--flood/--mask-file` | 视觉红遮罩 + `--mask-primary`，实测保护区保持率 99.8% |
| 反向保护（涂哪保哪） | `zimage.py edit --protect` | 保护区为原像素硬贴，适合保脸换装 |
| 确定性本地处理 | `zimage.py local` | 线稿调淡/放大/裁切/拼版/调子报告，不调接口 |
| 计划与预算预检 | `zimage.py plan --plan-out` | 生成可复用 `plan.json`（哈希、指纹、预算），不发送 |
| 状态与验收 | Job manifest | 请求指纹缓存、原子保存、完整解码、保护区指标、`NEEDS_REVIEW` 语义待审 |
| 手涂遮罩（可选） | `zimage.py serve` | 本地网页涂遮罩 → 导出 PNG → 回 CLI |
| 无限画布（可选） | `zimage.py board` | 即梦式：放图、加字、写提示词、生成贴回；仍走 `run_round` |
| 成果画廊（可选） | `zimage.py gallery` | 八分类页签 + 灯箱 |
| 风格蒸馏方法论 | `style-distill/` | 七条铁律、开工四问、失败态词库、L0/L1/L2 经验分级 |

## 架构

```text
用户自然语言 + 图片
        │
        ▼
  AI 任务解析（AGENTS.md 路由契约）
  ├─ deterministic_local ──► round_lib/local_ops.py（不调接口）
  ├─ localized_edit ───────► zimage.py edit（遮罩路径）
  └─ whole/pose/style ─────► style-distill 编译提示词 ──► zimage.py edit --whole
        │
        ▼
  zimage.py（预检 → plan.json → fingerprint → 缓存/Job 状态机）
        │
        ▼
  round_lib/run_round.py（体积门禁 → 压缩/JPEG → 发送 → 探测式取回 → 完整解码）
        │
        ▼
  结果验收（技术指标 + 保护区指标 + 语义待审） → 原子保存 → 缓存 → 经验登记
```

- `compose/images/mask_edit_app.py`：本地改图/画廊服务本体（也是 DSH 插件的服务端）。
- `style-distill/round_lib/run_round.py`：实测主力执行器（体积门禁、重试、探测式下载）。
- `zcode-image-edit/`：ZCode 原生技能 + CLI + 斜杠命令（推荐入口，运行期不依赖 DSH）。
- `dsh-plugins/dsh-image-edit/`：DSH 侧栏按钮参考实现（可选，不参与 ZCode 运行路径）。

## 快速开始

```powershell
# 1. 依赖：Python 3.14+，Pillow / numpy / opencv-python
pip install pillow numpy opencv-python

# 2. 凭据（只走环境变量，不写入任何文件）
$env:RELAY_API_KEY = 'sk-...'                                  # 当前会话
[Environment]::SetEnvironmentVariable('RELAY_API_KEY','sk-...','User')   # 永久
# 可选：RELAY_BASE_URL（默认 OpenAI 兼容 /v1 端点）、RELAY_MODEL

# 3. 自检（不调接口、不花钱）
python zcode-image-edit/bin/zimage.py doctor

# 4. 第一次改图（先看预算，不发送）
python zcode-image-edit/bin/zimage.py edit --image 原图.png --rect 300,200,700,600 `
    --prompt "把这块改成平滑灰绿渐变" --out 结果.png --dry-run

# 5. 确认无误后真跑（去掉 --dry-run）
```

技能与斜杠命令（可选）：`python zcode-image-edit/install.py` 装到 `~/.agents/`。

## 安全模型

- **凭据**：只从 `RELAY_API_KEY` / `RELAY_BASE_URL` / `RELAY_MODEL` 环境变量读取；缺失时在发送前明确报错，绝不白跑压缩流程。
- **不盲信 HTTP 200**：结果必须完整解码 + 尺寸核对 + （局部改图）保护区保持率 ≥98% 才算通过；整图结果标记 `NEEDS_REVIEW`，语义验收由 AI 视觉复核后登记。
- **可复现**：每次任务有 Job ID + manifest（输入/遮罩/提示词哈希、指纹、耗时、指标），敏感字段自动脱敏。
- **防重复扣费**：相同请求先查指纹缓存；计划文件默认不可静默覆盖，输入变化即失效。

## 已验证 / 未验证（如实清单）

已验证（本仓库开发过程中的实测记录，证据见 docs/）：
- 局部改图保护区保持率 99.8%（修复遮罩提示词缺陷前仅 29.1%）；
- JPEG 投喂同尺寸体积约为 PNG 的 1/5，可避开内容模糊压缩档；
- 指纹缓存命中不调用上游、无需凭据；损坏缓存自动拒绝；
- b64 路线返回尺寸可能不等于请求尺寸（工具按实际尺寸验收）；
- 三项本地门禁：可靠性测试 0 failures / 提示词校验 0 未处置 FAIL / 画廊分类 64/64。

未验证 / 已知限制：
- 【未验证】上游对其他模型/端点的行为差异；
- 【未验证】`--grabcut` 对动漫插画的效果（备选 `--flood`/`--polygon`）；
- 【判断】纯文本提示词对小结构（手、细链、饰品）有保真度天花板，需升级局部重绘。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/迁移说明A_会话与技能.md](docs/迁移说明A_会话与技能.md) | 会话总结、`style-distill` 技能规范（铁律/四问/体积实测） |
| [docs/迁移说明B_实现原理_改图与画廊.md](docs/迁移说明B_实现原理_改图与画廊.md) | 改图服务、遮蔽原理、画廊分类、模型接入 |
| [docs/环境_Windows与WSL2混用.md](docs/环境_Windows与WSL2混用.md) | Windows/WSL2 双环境实测结论 |
| [docs/开源工具与项目应用.md](docs/开源工具与项目应用.md) | GitHub 开源工具接入与验证：WD14 打标、colorgram 调色板、Lineart 线稿（含证伪记录） |
| [docs/Grok通道建立.md](docs/Grok通道建立.md) | Grok 通道建立：双引擎架构、实测约束、`input_fidelity=low` 画风迁移、三组对照实验 |
| [docs/响应中断与安全恢复.md](docs/响应中断与安全恢复.md) | 单次 POST 语义、结构化传输报告、安全恢复入口、离网测试 |
| [zcode-image-edit/README.md](zcode-image-edit/README.md) | CLI 用法、AI 主流程、安装 |

## 兼容性说明

- Windows（PowerShell / cmd）为主环境；`tools/wsl/` 提供 WSL2 薄通道的实测脚本。
- 上游为 OpenAI 兼容的 `/images/edits` 图生图端点；模型能力差异见文档 B 的实测账本。

## License

[MIT](LICENSE)
