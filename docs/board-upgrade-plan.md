# /board 画布能力升级方案(参照 basketikun/infinite-canvas)

> 2026-10-11 制定。背景:对比了 basketikun/infinite-canvas(已本地试运行)与我们的 `/board`
> (compose/images/board.html + board_app.py,路由在 mask_edit_app.py),决定按期移植交互能力。

## 目标

把 infinite-canvas 验证过的画布交互能力,分期移植进 `/board`,让"管线的图形前端"达到
通用工作台的操持手感——**只搬交互,不搬架构**。

## 不变量(每一期都不得违反)

1. 生成链路不动:`board_app.py` 的任务编译、`zimage.py edit`、GPT/Grok 双通道、路径白名单、
   dry-run、`ui_policy: explicit_board_submit` 全部保持原样;
2. `board.json` 向后兼容:新字段只增不改义,旧文件必须能直接加载;
3. 网页仍是展示/提交入口,不是第二个发送器(AGENTS.md 不变量 7);
4. 每期独立可验收、独立可回滚(纯前端期只改 board.html,刷新即回滚)。

## 现状基线【文件核实,2026-10-11】

已有:双击/端口拖线新建节点、连线解析图一图二、遮罩矩形、提取节点编卡 III、
Shift 框选多选、多选拖拽删除、空格平移、滚轮缩放、拖文件上传、网页内 git 更新。
缺:撤销/重做、小地图、多画布、复制粘贴、成组、资产面板、主题、快捷键说明。

## 分期

- **P0 救命与导航**(只改 board.html):撤销/重做(全量快照栈 50 步,Ctrl+Z / Ctrl+Shift+Z /
  顶栏按钮)+ 小地图(右下角 canvas,按 nodeBox 画包围盒 + 视口矩形,点击/拖动跳转)。
- **P1 编辑效率**(只改 board.html):Ctrl+C/V 复制粘贴(带内部连线,偏移 36px)、
  快捷键浮层(`?` 呼出)、深色主题(CSS 变量 + localStorage)。
- **P2 多画布**(board.html + board_app.py + mask_edit_app.py):`.zimage/board/boards/<id>.json`,
  旧 board.json 自动迁移为 boards/default.json;后端 list/create/rename/delete;
  前端顶栏画布切换;删除不允许删最后一块。
- **P3 素材与整理**(前端 + 只读后端):资产面板浏览 `产图/`(懒加载、点击落画布中心);
  成组(type:'group' 容器,Ctrl+G 打组 / 拖动带成员 / 删组不删成员)。

## 明确不做(及理由)

浏览器直连 AI 与渠道配置 UI(违背唯一 owner/任务契约)、IndexedDB+WebDAV(数据已在 git 仓库)、
视频/音频节点(无后端)、Agent 面板(agent 是 ZCode 会话本身)、第三方提示词市场直连
(可日后做本地版提示词库,单列需求)。

## 风险与决策点

1. 单文件膨胀:P0–P3 后 board.html 约 2.4k 行;超过 2.5k 行时拆出 board.js 静态文件,不上框架;
2. 撤销栈内存:全量快照约几十 KB/步,50 步封顶;
3. 向后兼容:新字段(group、boards)对 load_board() 字段合并逻辑透明;
4. 提交节奏:每期由用户验收后提交;网页"检查更新"要求工作区干净。

## 验收方式(每期相同)

在运行中的 /board 实测(改 board.html 刷新即生效)→ 按 AGENTS.md 不变量逐条核对 →
浏览器结构快照 + 截图双验收 → 记录【实测】结论。

## 验收记录(2026-10-11,全部【实测】)

- **P0**:撤销/重做全链路(新建→撤销→重做→删除→撤销恢复;文本输入节流入栈;拖拽/缩放
  首次移动才入栈);小地图对象分色 + 视口框 + 点击跳转 + 空画布自动隐藏。截图确认。
- **P1**:复制 2 节点→粘贴 +2 节点 +1 内部连线(id 重映射正确)、gen 表单字段完整保留、
  偏移 36px;`?` 快捷键面板开合;深色主题切换 + localStorage 持久化。
- **P2**:创建/改名/删除 API 通过;两块画布保存隔离(default 0 节点 vs 测试板 1 节点);
  最后一块删除被拒;非法 board id(路径穿越)返回 400;board.json 自动迁移为
  boards/default.json。
- **P3**:资产面板列出 产图/ 全部 195 张(路径全部过 allowed_path 白名单),点击落画布
  中心;成组(Ctrl+G)→成员 o.group 关联→拖组带成员(模拟数据层验证)→删组不删成员→
  拆组(Ctrl+Shift+G / 双击组框)→撤销全清。深色主题 + 组框 + 资产面板组合截图确认。
- 过程中发现并修复 1 个 bug:ensureEl 组分支引用了不在作用域的 cap 变量(改为 DOM 查询)。
- 结案状态:画布恢复空置(0 节点),主题恢复浅色。改动文件:compose/images/board.html
  (1169→1613 行)、compose/images/board_app.py(+约 120 行)、
  compose/images/mask_edit_app.py(路由 +6 个)、docs/board-upgrade-plan.md(本文件)。
- 不变量核对:build_edit_cmd / run_edit / run_extract 未动;生成链路仅增加 board_id 传参;
  路径白名单、dry-run、任务契约、ui_policy 原样;board.json 旧文件经迁移函数兼容。
  遗留决策点:P2 后如需网页"检查更新",需先由用户提交这些改动(工作区现为 dirty)。

## P4:Board MCP server(2026-10-11 追加,全部【实测】)

- `compose/images/board_mcp.py`:stdio MCP server(JSON-RPC 2.0,换行分隔,零第三方依赖),
  6 个工具:`board_list / board_read / board_add_text / board_add_image / board_wire / board_budget`。
- **治理边界(刻意设计)**:不提供真实生成工具——agent 只能"备料"(摆节点/连线/预算),
  真实提交仍由人在页面点「生成」(ui_policy: explicit_board_submit)。add_image 走
  allowed_path 白名单;board_budget 只跑 dry-run,不出网。
- 并发安全:`save_board` 增加 `expected_mtime` 乐观锁参数(网页端不受影响),MCP 写入
  冲突自动重读重试 3 次。
- 注册:`.zcode/config.json` → `mcp.servers.board`(工作区级,新会话自动连接)。
- 协议实测:initialize/tools/list/6 个工具全过;白名单外路径被拦;wire 幂等;
  测试节点已定向清理,用户自己的节点未动。
- 已知限制:同名画布被网页与 MCP 同时写入时,后写者以乐观锁感知冲突重试;画布内独立
  对话面板(不经 ZCode 调文本模型)是另一个功能,未包含。

## P5:预设工作流(2026-10-11 追加,【实测】)

- 左栏新增「画风」「姿态」两个预设按钮;双击空白菜单同步新增两个预设项(支持从端口
  悬线进入时自动接进预设)。
- 一次点击创建:提取节点(kind=style/pose)+ 生成节点 + 提取→生成连线,并显示四步
  操作指引(接参考图→提取→接图一→生成);真实提交仍由人工点「生成」。
- 撤销语义:整个预设只占一条撤销记录(undoSuppressed 计数器抑制内部子步骤入栈)。
- 实测:两个预设 kind 正确、连线建立、单次撤销撤整组;用户既有节点未受影响。

## P7:画风蒸馏 + 确定性对色(2026-10-11,【实测】)

回应"要真实模仿画风,而非单纯模拟色调"——机制上回答"画风从哪来":

- **蒸馏引擎** `compose/images/distill_style.py`:对一张参考图产出证据包——
  ① `lineart.png`(controlnet_aux LineartDetector 本地模型抽出的笔触结构层);
  ② `exemplar-N.png`(按边缘密度自动选区的笔触样例裁片,脱离人物全身上下文);
  ③ `tone.json`(LAB 均值/方差指纹);④ `style-card.md`(画布同模板画风卡)。
  全程本地,不请求上游。
- **多参考生成**:`build_edit_cmd` 扩展——除图一外,所有连到生成节点的图片
  (图二 + 蒸馏样例/线稿)经 `--ref`(image[2..])上传,模型直接看到参考像素。
  这是"真实模仿画风"的主通道;文本卡是护栏与参数,不是唯一信号。
- **确定性对色**:生成后按蒸馏包的 LAB 指纹做 Reinhard 色调迁移(本地数学),
  色调部分不再依赖模型猜测。往返恒等误差 0(修掉一个 Y 分量未立方的 bug 后);
  跨图对齐 LAB 三通道全部收敛到目标。
- **入口**:左栏「蒸馏」(选中画风源图)、「对色」(选中成图);产物自动落画布
  (画风卡文本节点 + 线稿/样例图片节点,均带 tone_src/tone_json 引用)。
- **实测**:对用户参考图蒸馏全链路通过(线稿目检合格,4 样例,卡 1094 字);
  多参考 dry-run 命令含 2 个 --ref;临时画布前端 e2e 通过后已删除。
- **已知取舍**:笔触样例按边缘密度选区,可能选中面部(含五官特征)——
  它同时是笔触证据与身份信息,靠卡内 DO NOT INCLUDE 与生成后验收把关;
  后续可加"避开人脸"的选区约束(未做)。

## P8:审查修复轮(2026-10-11,全面审查后的 P0-P6 落地,均【实测】)

全面审查发现并修复的问题与新增能力,按执行方案的 P0-P6:

- **P0 数据与保存安全**
  - `run_extract(board, node_id, bid)` + 路由传 bid:非 default 画布提取不再串写
    【实测:临时画布提取后 default 三节点/两线未变】。
  - 网页保存加入 revision 乐观锁:`/api/board` 返回 `_revision`,`save` 带
    `expected_revision`,冲突返回 409,页面显示"发生冲突"而非假"已保存"
    【实测:旧 revision 保存 → 409 conflict】。revision 检查与原子替换置于
    msvcrt 跨进程文件锁内,与 MCP 的 mtime 锁并存。
  - `save()` 检查 HTTP/ok/conflict/网络异常,状态分 保存中/已保存/保存失败/发生冲突。
  - 「修复角色引用」按钮 + 生成前阻止:`resolve_roles` 不再从过期 identityId 猜图一;
    无有效图一时预算明确报错【实测:browser + API 双验证】。
- **P1 语义合同**
  - `zimage-task-v1` 新增 `scopes`(identity/pose/style/color)与完整
    `input_roles`(identity/style_reference/pose_reference/lineart_reference/
    exemplar_reference),`preserve` 按 scope 生成【实测:task.json 断言】。
  - `reference_inputs()`:直接连生成节点的图 + 经提取节点进入的图,按提取 kind
    继承 style/pose_reference 角色;身份图自动排除。
  - 蒸馏卡拆分 `model_prompt.md`(无 hex、无 DO NOT INCLUDE、调色板改语言描述)
    与 `style-card.md`(含 HUMAN_REVIEW);生成节点只收模型段
    【实测:model_prompt 无 hex/无负面块】。
- **P2 蒸馏包审核闸门**
  - 蒸馏包新增 `manifest.json`(source_hash/scopes/identity_risk/approved)与
    `source-preview.png`;人脸检测(cv2)排除脸部窗口,无法检测时 risk=unknown。
  - 样例默认 `distill_approved=false`:未审核样例连到生成节点时预算被阻止;
    「审核蒸馏包」按钮显式批准后才可作为参考;risk=high 拒绝批准
    【实测:未审核阻止 → 批准 → 预算通过】。
  - 已知限制:正面人脸 Haar 对动漫脸检出率有限,未检出时 risk=unknown,
    依赖人工点击批准这道闸。
- **P3 真实姿态估计**
  - `pose_extract.py`:本机 `controlnet_aux.OpenposeDetector`(权重经 hf-mirror
    下载成功),输出 `pose.json`(归一化关键点)/`pose-overlay.png`/`pose-card.md`,
    明确 `pose_estimated=true / pose_controlled=false`【实测:骨架图目检,关键点
    与半身裁切一致,下半身正确返回 None】;画布新增「姿态估计」按钮。
    曾尝试切换 DWPose(精度更高):其人体检测依赖 mmdet/mmcv,Python 3.14 无
    mmcv 轮子,安装受阻,已回退 OpenPose 并如实记录;DWPose 权重下载本身经
    hf-mirror 可用,待 mmcv 支持 py3.14 后可再切。
  - 权重不可用时明确失败,不生成假姿态数据【实测】。当前中转站仅把姿态产物当
    附加参考图,不声称骨骼控制。
- **P4/P5 预设与语义门禁**
  - 两个预设自动设 `input_fidelity=low` 与 scope(画风: style=transfer;
    姿态: pose=transfer;身份/配色 preserve)。
  - 预算语义门禁【实测】:style_scope=transfer 无 style_reference → 阻止;
    pose_scope=transfer 无 pose_reference → 阻止;迁移 scope 下 fidelity≠low → 阻止;
    未审核样例 → 阻止;Grok 多参考 → 阻止。
- **P6 生成后验收**
  - 结果节点记录 `job_id`;「验收结果」按钮逐项收集
    requested_change/identity_preservation/style_transfer/pose_transfer/
    reference_leakage/color_scope 的 pass/fail/uncertain,写入既有
    `zimage.py review` 合同【实测:错误路径;真实登记待首次生成】。
- **对色授权**:`align_tone` 需显式 `authorize_color_transfer` 且
  `color_scope=transfer`,否则拒绝【实测】;前端对色前 confirm。
- **其余修复**:图一/图二按钮进撤销历史;切换/新建/删除画布前 flush 保存,
  保存失败阻止切换。
- **遗留**:① 真实生成对照实验待用户执行(费用归属用户);② OpenPose 权重经
  hf-mirror 下载,主站不可达;③ 动漫脸 Haar 检出率有限;④ 旧 `board.json`
  遗留文件与 boards/ 并存(仅历史);⑤ workspace 仍未提交,网页"检查更新"
  需先提交。

## P9:首次真实生成验证(2026-10-11,【实测】)

用户授权后执行单变量真实生成(只改画风,一次 POST,`gpt-image-2.5-sunburst`):

- **配置**:图一=`2ce1e9735f724b08af5c5eb395559d4a.png`(丸子头/黑高领/紫西装/手表/
  耳环/文件夹,与画风源不同身份);style_reference=长卷发参考图(经提取节点的画风卡);
  `input_fidelity=low`;`style_scope=transfer`,`color_scope=preserve`。
  同时完成 P0.4 角色修复(过期 identityId → p6-ident)。
- **门禁**:dry-run 全绿后才真实提交;task roles =
  [p6-ident:identity, wjra6dwj:style_reference]。
- **结果**:`产图/board-20261011-191808-25a1f75186.png`。
- **语义验收【目测,已登记 PASS】**:identity_preservation=pass(丸子头/发带/高领/
  西装/手表/耳环/文件夹/持笔姿态全部来自图一);style_transfer=pass(细线、留白、
  草图感、低饱和处理向参考图靠拢);reference_leakage=pass(参考图人物的长卷发/
  白衬衫/灰西装未出现);color_scope=pass(图一配色未被染色);pose_transfer=pass
  (preserve 语义,无姿态迁移需求)。
- 结果节点已落回画布(带 job_id,可复核);job manifest:
  `.zimage/jobs/board-20261011-191808-25a1f75186/`。

## P10:多图画风蒸馏(2026-10-11,【实测】)

回应"多张图连到提取节点能否聚合蒸馏"——从单图采样升级为跨图分布逼近:

- **蒸馏引擎** `distill_images()`:CLI 接受多张源图(`nargs="+"`),逐图量测/线稿/
  样例(每图 K 张,人脸检测避让),跨图聚合——调色板按图均权合并重排、
  边缘/纸白取均值并报告跨图波动、WD14 身份标签取并集(多图共现特征才入
  DO NOT INCLUDE)、LAB 指纹按像素数池化。
- **画风卡**:标注"跨 N 张样本聚合"+ 跨图波动区间(波动大=样本画风不稳定,
  提示人工复核);模型段继续隔离(hex/负面块剥离)。
- **提取节点多图**:`run_extract` 对所有连线图片逐张量测后聚合编译,节点记录
  `sources[]`,单图行为不变。
- **高风险人工通道**:`approve_distill` 拒绝 risk=high 包,但提供
  `allow_high_risk` 人工覆盖(前端二次确认),manifest 记录
  `approved_high_risk=true / approved_by=human-override`——自动路径仍封锁,
  人工审查后可用。
- **实测**:3 张样本(2 实图 + 1 线稿)蒸馏 → 3 线稿/9 样例/聚合卡
  ("跨 3 张样本聚合"出现,模型段无 hex);提取节点 3 图聚合 → MULTI(N=3) +
  sources 3;未审核样例预算阻止 → 高风险包人工覆盖批准 → dry-run task 含
  identity + 2×style_reference + exemplar_reference,3 个 --ref,fidelity=low。
- **验证教训**:曾误报"无 style_reference"——根因是测试画布漏连 mx→g 边,
  门禁行为正确;reference_inputs 单测确认含提取链路继承角色。

## P11:画风库(2026-10-11,【实测】)

蒸馏包从"散落目录"升级为可管理的资产库:

- **API**:`GET /api/board/styles`(列出全部包:名称/样本数/风险/审核状态/
  预览图/画风卡文本/线稿样例清单)、`style-rename`、`style-delete`
  (仅限 distill 目录内,防路径逃逸)。
- **画布**:左栏新增「画风库」抽屉——每个包显示预览缩略图、名称、
  样本数/风险/审核徽标;操作:应用到生成 / 批准(高风险二次确认)/ 改名 / 删除。
- **一键复用**:「应用到生成」= 画风卡文本节点 + 线稿/样例图片节点全部自动连到
  生成节点,并自动设置 `style_scope=transfer / input_fidelity=low`——
  蒸馏一次,此后任意生成一键套用,不必重跑蒸馏。
- **实测**:临时画布应用已批准的 3 样本包 → 13 节点落位、全部正确连到生成节点
  (1 卡 + 3 线稿 + 9 样例)、scope/fidelity 自动设置;旧单图包的 source 计数
  兼容回退已修;应用测试误入真实画布的节点已清理(undo/清理流程验证)。
- **用户实测**:用户随即以 13 张样本跑聚合提取,MULTI(N=13) 卡正常生成。

## P12:三类样例蒸馏(2026-10-11,【实测】)

回应"更深层次蒸馏"——样例从边缘密度碰运气升级为**分类证据集**:

- **三类样例**:per_source 产出
  `exemplar-<i>-stroke-*.png`(笔触:细线/勾线高频纹理区,低风险)、
  `exemplar-<i>-fabric-*.png`(材质:通道间色彩差异最大区,低风险)、
  `exemplar-<i>-face-*.png`(五官:面部画法区,**高风险**)。
- **人脸定位**:OpenPose 面部关键点(动漫可用)为主,Haar 回退;既用于排除
  笔触/材质选区,也用于生成五官样例裁片。修复两个实现 bug
  (closed-image 读图、colorvar 多余 mean)。
- **门禁联动**:含 face 样例的包 identity_risk=high,批准需二次确认
  (allow_high_risk,manifest 记录 human-override);未审核样例预算照旧阻止。
- **实测**:双图蒸馏 → stroke×4 / fabric×4 / face×1,manifest/model_prompt/
  聚合卡(MULTI(N=2) + 波动行)齐全,模型段无 hex;identity_risk=high。
- **定位如实**:这是 gpt-image 后端上"提示级+像素证据级"的深度上限;
  权重级内化(LoRA)需本地 SD/Flux 通道,非本管线可解。
