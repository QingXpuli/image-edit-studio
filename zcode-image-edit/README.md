# zcode-image-edit · 在 ZCode 里改图（不含 DSH 依赖）

把原本挂在 DSH Web GUI 侧栏上的「改图 / 图片」能力搬进 ZCode。**运行期不需要 DSH**。

## 它为什么这么薄

**不重写任何链路**。发送、压缩、体积门禁、断连重试、探测式抢档下载、完整解码校验，
全部委托给 `style-distill/round_lib/run_round.py`（那是本项目用血换来的主力脚本）。
本目录只补三样 DSH 插件曾经提供、而 ZCode 没有的东西：

| 原来由 DSH 提供 | 这里的替代 |
|---|---|
| 鼠标刷子涂遮罩 | `mask_gen.py`：可编程的区域规格（矩形／多边形／漫水／前景分割）；**网页手涂也保留了**，用 `serve` 拉起 |
| 侧栏按钮 + 懒启动 | `zimage.py serve / gallery / stop`：直接拉起本地服务、开浏览器、按 pid 停 |
| 页面上手填 API Key | 环境变量（`RELAY_API_KEY` / `RELAY_BASE_URL` / `RELAY_MODEL`），**不落盘、不进仓库** |

## 安装

```
python zcode-image-edit\install.py            # 装技能与命令，并自检
python zcode-image-edit\install.py --check    # 只看状态
```

装到 ZCode 的用户作用域：技能 `~/.agents/skills/image-edit/`、命令 `~/.agents/commands/`。
装完**重启 ZCode 会话**才会被发现。

`install.py` 会按 ZCode 的**静默丢弃规则**先校验源文件（frontmatter 缺 name/description、
description 超 1024 字符、命令名不合规、命令既无描述又无正文）——这些情况加载器不报错、
只是不出现，光看文件在不在是发现不了的。

## 凭据

```
$env:RELAY_API_KEY = 'sk-...'                                    # 当前会话
[Environment]::SetEnvironmentVariable('RELAY_API_KEY','sk-...','User')   # 永久
```

`RELAY_BASE_URL`（默认 `https://image-direct.geiliapi.com/v1`）与 `RELAY_MODEL`（默认
`gpt-image-2`）可选。**没有 key 时命令会在发请求之前就停下并给出设置方法**，不会白跑一遍压缩。

已实测的安全断言：环境变量里的 key 只进服务端兜底，**不会出现在渲染给访问者的页面里**。

## AI 主流程与用法

AI 默认负责任务分类、输入图角色分配、提示词编译、预检、提交和结果验收；网页只用于复杂遮罩绘制、画布或画廊，不是默认生成入口。先判断能否走 `local`；`prompt_only`、inspect、gallery、dry-run 不发送。身份、内容和配色迁移按用户 scope，且不默认 `input_fidelity=low` 保身份。

> 当前 policy：[`../docs/current-policy.md`](../docs/current-policy.md)。本次仅更新仓库文档与技能，runtime 安装副本未更新；旧 relay 数字仅作历史证据。

```
zimage.py doctor                       # 自检，排查第一步（不调接口）
zimage.py plan --image 原图.png --whole \
               --prompt-file prompt.txt --plan-out .zimage/plan.json
                                         # 生成可复用本地计划，不发送
zimage.py edit --plan-file .zimage/plan.json \
               --out 结果.png --dry-run  # 复用计划先看预算，不发送
zimage.py edit --plan-file .zimage/plan.json \
               --out 结果.png            # 去掉 --dry-run 真跑
zimage.py local tone-report 结果.png    # 确定性本地操作，不调接口
zimage.py serve / gallery / stop         # 网页仅作手涂/画廊辅助
```

`plan` 会原子写入 `zimage.plan` artifact、提示词快照和（有区域时）持久化遮罩；默认拒绝覆盖已有计划，需显式 `--force`。执行时会校验源图、参考图、提示词、遮罩和 fingerprint 未变化。

区域规格：`--rect x0,y0,x1,y1`（可重复）｜`--polygon "x,y x,y x,y"`｜`--flood x,y[,tol]`
（种子点漫水，换纯色背景最趁手）｜`--grabcut`｜`--mask-file M.png`｜`--whole`（整图档位）。

语义：默认**正向**（圈中的被改、其余物理不动），会自动带 `--mask-primary`——实测不加它时
模型会照抄一起发过去的干净原图把那块"恢复"、返回近乎原图。加 `--protect` 则**反向**
（圈中的被保护、其余重建），此时**不要**配 `--mask-primary`。

`--dry-run` 除了预算报表，还会把遮罩与 `run_round` 写出的涂红图落在输出旁边
（`<out>-region.png` / `<out>.redmark.png`）——**发送前看一眼红色落在哪一块**。

## 画廊的「输入」页签

DSH 版索引读会话日志；ZCode 把上传的图**以 base64 data URL 存成 artifact 文本文件**、
元数据在 SQLite，所以数据源必须换：

```
python zcode_inputs.py          # 生成 style-distill/round_lib/user_inputs_zcode.json
set GALLERY_USER_INPUTS=<工作区>\style-distill\round_lib\user_inputs_zcode.json
zimage.py gallery
```

输出的 schema 与 DSH 版**完全一致**（`paths` / `excluded` / `unreachable`），
所以画廊的 64 条分类回归断言**一条都没动**。

**报 0 条是如实结果，不是故障**——本机 ZCode 的 `session_input.payload.attachments` 目前
全是空数组（实测：本项目 12 行 `sendText` 全空、`input_history` 非空 0 行、artifacts 里
94 个文件全是工具结果）。在 ZCode 对话里真发一张图，重跑 `zcode_inputs.py` 就有了。

## 已验证 / 未验证

【实测】本地 plan artifact、持久化遮罩、计划复用、覆盖保护、缓存命中与测试隔离已通过 `zcode-image-edit/tests/test_reliability.py`；此前已有三次真实整图多图参考生成记录。

已验证（均为本地，不花额度）：
- `mask_gen.py` 三种区域规格：涂红区域与请求区域**逐像素吻合**，未改动区域与原图**逐像素一致**；
- 凭据层：无 key 时在发请求前终止（rc=2）并给设置方法；`--dry-run` 不需要 key；
- `edit --dry-run` 局部与整图两条路径：预算报表、遮罩、涂红图都产出；
- `serve / gallery / stop`：HTTP 200、复用已在跑的服务、停后端口释放；
- 服务渲染页面**不含**环境变量里的 key；
- 技能与命令安装后**逐哈希一致**，且通过 ZCode 的丢弃规则校验。

未验证：
- 【未验证】当前 CLI 正向/反向遮罩的真实 Job 闭环（底层 `run_round.py` 曾有历史真实遮罩证据，但本轮只做离网验证）；
- 【未验证】整图身份/姿态的自动语义验收，目前仍需 AI 视觉复核；
- 【未验证】画廊「输入」页签在 ZCode 侧的端到端表现；
- 【未验证】`--grabcut` 对动漫插画的效果未做对照，可能不如 `--flood` 稳。

## 与 DSH 插件的关系

DSH 插件（`dsh-plugins/dsh-image-edit/`）**保持不动**，不参与本目录的运行路径，
留作参考实现。要让 DSH 也不再自动拉起本地服务，把它的 profile 依赖与
`dsh.profile.bundles` 条目去掉即可（本目录不代劳）。
