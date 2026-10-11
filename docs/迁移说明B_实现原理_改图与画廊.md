> **Historical warning（2026-10-08）**：本文保留原始迁移/实测记录，不是当前默认 policy。当前路由、生成授权、验收与审批边界见 [`current-policy.md`](current-policy.md)；尺寸、速度、retry 502、b64、物理硬贴和模型 fallback 需按当前环境重新验证。

# 迁移说明 B · 实现原理（改图插件 + 图片画廊）

> 口径标注同文档 A：**【实测】**／【文件核实】／【判断】／【未验证】。
> **本文档可单独转发**：插件包与本文档**零密钥**（密钥只在运行时于页面手填）；
> `mask_edit_app.py` 曾含密钥，**已于 2026-09-25 全仓库清除**（39 处 / 22 文件，见 B11）；
> 但其 **git 历史里仍是明文**，对外发布仓库前务必轮换 key。

## B1. 两个挂件是什么

给 DSH Web GUI 的侧栏加两个按钮：

- **「改图」**：打开本地网页，**在图上涂抹要改的区域** → 填 Base URL / API Key / 模型 + 提示词 →
  调中转站图生图接口出结果。**点一下才启动本地服务**，不用事先开终端；服务起不来时按钮上直接显示原因。
- **「图片」**：打开本地**图片画廊**（分类页签 + 页内灯箱预览）。

**为什么要自己做**：聊天客户端与中转站控制台都**不给你画遮罩的地方**，而这套接口的"局部改图"恰恰要靠遮罩。

## B2. 三块架构与调用时序【文件核实】

```
① 浏览器（DSH Web GUI，同源 http://127.0.0.1:3080）
     lib/client.js → 往槽位 sidebar.footer.action 注册两个按钮（同槽位 id 必须不同）
     点击 → fetch 同源路由 POST /dsh-image-edit/ensure
② 节点半边 lib/index.js（跑在 DSH 宿主进程）
     3 条路由：POST /ensure（探活+按需拉起）、GET /status、POST /stop
     拉起：spawn(detached, stdio:'ignore', unref) 启动 Python；pid 落盘 .dsh-image-edit.pid
③ 本地服务 mask_edit_app.py（独立进程，127.0.0.1:8000）
     GET  /            改图页（涂抹画布 + 参数面板）
     POST /api/ping    连通性 + 可用模型数
     POST /api/models  拉模型列表
     POST /api/edit    组装并转发到中转站 /images/edits
     GET  /gallery     图片画廊
     GET  /gallery/img 图片字节（白名单根目录 + 后缀校验）
```

文件与行数【实测】：`lib/client.js 243`、`lib/index.js 311`、`package.json 41`、
`cordis.patch.yml 40`、`README.md 264`、`_test_client_button.mjs 204`、`_test_lazy_start.mjs 156`、
`compose/images/mask_edit_app.py 2896`。

## B3. 设计取舍（每条都是踩出来的）【文件核实 + 实测】

| 决策 | 原因 |
|---|---|
| **懒启动**，不要求先手动开服务 | 常态不需要它；按钮点那一下起最省事。pid 落盘 → DSH 重启后 `/stop` 仍可用 |
| 用 `spawn(detached)` 而**不是** `ctx.subprocess` | subprocess 这个 seam 会在服务销毁时**连带杀掉**其管理的进程，而本地服务应当比 DSH 活得更久 |
| 客户端半边**只依赖 `slots`** | `inject` 里 await 不存在的服务会让条目**永远 pending**；且 `apply` 抛异常会**fail 整个 web-shell 启动** |
| 探活用 `node:http` 而**不是** `fetch` | **实测**：对以 HTTP/1.0 应答的服务 abort 一个 fetch，会在 undici 内部抛**不可捕获**断言（来自 socket 事件处理器）→ **杀掉整个 DSH 宿主进程**（= GUI 挂掉） |
| 路由挂在 `/api` **之外** | `/api` 前缀被 `dsh-client-connection` 的浏览器信任栅栏独占 |
| 改 `lib/index.js` **必须重启 `dsh web`** | node 半边只在 boot 时加载；`patchReload: "live"` 只管 patch 文件 |
| 改 `lib/client.js` 只需刷新页面 | 客户端半边由 `/plugins/<id>/client.js` 提供。**注意：本条未在浏览器里验证过**（见 B9【未验证】） |
| 本地服务模块级用到的模块**必须在模块级 import** | **实测**：`os` 只在函数内 import 而模块级写 `os.path.join(...)` → 启动即 `NameError`，表现为"按钮点了只报 503"。修复方式是启动自检：`python mask_edit_app.py --port 8011` |
| 服务是**独立进程**，改它不必重启 DSH | `POST /stop` → `POST /ensure` 即可（curl 两条命令） |

## B4. 遮蔽是怎么做到的（核心发现）【实测 + 文件核实】

1. `/images/edits` 是 multipart，但它的 **`mask` 字段被静默忽略**（发过去 0% 效果）
2. 真正被认的是：**把"要改的区域"硬涂成纯红，合成进 `image[1]`**，提示词里明确
   "红色只是标记、只有这一块要改、其余保持原样" → 保护区【实测】**100% 不动**；
   本项目另一处量到**保护区最大改动 0.0**
3. 代价：硬涂红把该区域原有信息**整块抹掉**，模型只能靠提示词重建 →
   适合"这块本来就要清掉重画"（换背景、重画道具），不适合"保留结构、整体加色"
4. **反直觉补充**：把**干净原图**一起发过去时，模型会照抄它把那块"恢复"，
   正向改图可能变成"近原图返回"；**只发涂红那张**才会真的重画 →
   页面上的"作用域 / 遮罩模式"就是在切这两种用法（命令行为 `--mask` 与 `--mask-primary`）

## B5. GPT 通道实测账本【文件核实，来自 `mask_edit_app.py` 头部实测注释】

- 结果以 **b64_json** 返回；**支持多图**（`image + image[1] + image[2]` 三张实测可用）
- **尺寸不严格**：请求 `1024x1536` 回过 `1029x1528`；`1024x1024` 回过 `1254x1254`
  → 本项目进一步查到：**`b64` 响应路线不守 `size`（3/3），`url` 路线精确命中**；别做像素级精确假设
- 账号只暴露**一个**模型（`gpt-image-2`），"获取模型列表"只显示一条是**正常**的
- `HTTP 502 {"Upstream access forbidden…"}` 是**瞬时上游故障**（坏请求是 400）→ **重试即可**
- **体积**：表单上限约 **1.96 MiB 通过 / 2.12 MiB 起被拒**；投喂体积由**目标尺寸**决定；
  **JPEG 投喂比 PNG 小约 5 倍**（2.80 → 0.56 MiB）

## B6. 接入你自己的模型 / 中转站

**上游需要**：OpenAI 兼容 `POST {base}/images/generations`（文生图）与 `POST {base}/images/edits`
（multipart，**遮罩玩法的基础**；没有它就只能文生图）；认证 `Authorization: Bearer <key>`；
可选 `GET {base}/models` 用于"测试"与"获取模型列表"（【文件核实】实际发法见 `mask_edit_app.py`）。

**页面上填三样**：Base URL（到 `/v1`）、API Key（**只存浏览器 localStorage**，带注入/清除开关）、
模型名。"测试"打 `{base}/models` 并回报可用模型数——最快的连通性验证。

**新增"引擎"预设只改一处**（`mask_edit_app.py` 顶部 `ENGINES`）：

| 字段 | 含义 | 影响 |
|---|---|---|
| `label` / `base` / `key` | 显示名 / 默认地址 / 默认 key | key 留空则要求页面手填 |
| `t2i_model` / `i2i_model` | 文生图 / 图生图各用哪个模型名 | 两者可不同名（本预设里同为 `gpt-image-2`） |
| `uses_quality` | 是否发 OpenAI 的 `quality` | 与 `uses_resolution` **二选一**，发错会被上游拒 |
| `uses_resolution` | 是否发 `resolution`（1k/2k） | 有些上游用它驱动输出尺寸 |
| `edits_b64` | `/images/edits` 是否回内联 base64 | 本预设 **true**；为 false 时必须能下载它回的 url |
| `multi_image` | 是否接受多张参考图 | 为 false 时多于一张会 HTTP 400 |
| `mask` | 是否支持"红标遮罩"玩法 | 为 false 时只能整图重画 |
| `sizes` | 各档位可选尺寸 | 填上游**真实接受**的组合 |

新增后**只需重启本地服务**（`/stop` → `/ensure`），不用重启 DSH。

## B7. 「图片」画廊

### B7.1 数据来源与八个分类页签【文件核实】

服务端渲染 HTML（`gallery_html()`），卡片带 `data-cat`。
**扫描根 = 只扫最新一轮**（`scan_roots()` → `latest_round()`：按目录 mtime 取 `style-distill/round_*`
中最新者，排除 `round_lib`）→ 落实用户口径："**只从最近这轮开始计入、之前的全部隐藏**"。

判据集中在 `gallery_category()` 一处：

| 页签 | 判据 |
|---|---|
| 成果 | 轮次根层 `out_*` **且该轮 README 里带 ✓**；或 `compose` 的 out/final/deliver 里名字不含 original/preview/bg_only/mask/side_by_side/tech… 的成品 |
| 候选 | 轮次根层 `out_*` 但 README 里**没有 ✓**（"待选"因此与"已认可"自动分开） |
| 输入 | **用户自己上传的图**（按 sha256 与会话日志配对，见 B7.2） |
| 参考 | 参考目录（`ref_xiami / pose_ref* / references`）、名字明示（`style_ref / pose_style / identity_ref …`）、`input/` 里 **B/D 前缀**（本项目角色分配约定：A/C＝内容图、B/D＝参考图） |
| 局部 | `round_*/lab` 局部改图实验件、涂红预览（`*redmark*`）、局部放大对照（`*_zoom*`）、蒙版素材（`mask_*`、`mask_preview`、`line_mask`） |
| 对照 | `checks/`、`diag/` 目录，或名字含 `compare / _vs_ / _cmp / side_by_side / grid / _sheet / contact / _ab_` |
| 过程 | 其余（`work/`、`input/` 的 A/C、`target*`、`round_lib`、`compose` 杂项、`_probe` 草稿区） |
| 全部 | 以上之和；**过程封顶 120 张**（成果/候选/输入/参考/局部/对照不封顶） |

### B7.2 「输入」页签：用证据而不是命名【实测】

- 从**会话日志**（`~/.dsh/sessions/--C-Users-typ-Desktop-mantu--/*/session.jsonl.zstd`，
  Python 3.14 内置 `compression.zstd`）提取 `role=user` 消息里的图片附件 → 跨 13 个会话共 **21 张**
- 与工作区图片做 **sha256 配对** → 13 个文件在仓库里；**只出现在最近一次会话**的 = **2 张**
  （`round_arcade/input/A_pose_env.png`、`B_char_style.png`）→ 这就是「输入」页签内容
- 索引固化在 `style-distill/round_lib/user_inputs.json`（由 `build_user_inputs_index.py` 生成，可复跑）
- **坑**：附件元数据的 `attachmentId` 与附件库对象文件名**不是同一个哈希**（对象是规范化后的副本），
  故那 **9 张"只在附件库"**的上传**无法在画廊里定位** → 不造假卡片，改为页头如实标注数量

### B7.3 分类修正史（用户一句质疑逼出的 4 类真错误）【实测 + 图证】

初版按路径拍脑袋分类；被问"是否仔细甄别"后，用**拼版看图 + 逐条打印清单 + 字节数比对**核对，查出：

1. **`round_lib` 被当成轮次目录** → 7 张测试产出误判为"成果"
2. **"成果"桶混进非成品**：`03_original.png`（原图）、`04_mask_guide.png`、`05_side_by_side.png`、`mask_preview.png`
3. **"局部"桶混进 A/B 实验件与投喂件**（只因子串含 "mask"）：`_cmp_B_no_mask.png`、`_sent_mask.png` 等
4. **"局部"桶混进冒烟残留与 23 张对照拼版**：`_smoke_crop/_smoke_sheet`、`checks/*`

关键旁证：`deliver/02_background_only.png`、`final/result_bg.png`、`out2/01_bg_only.png`
**三者字节数完全相同（340,810 B）** → 同一个背景层被复用，属**图层**而非成品。
修正后落成 **64 条回归断言**（`style-distill/assert_gallery_categories.py`）。

### B7.4 灯箱交互与三个前端坑【实测 + 文件核实】

交互：点缩略图页内打开 → **点空白 / 点最外层 / Esc 关闭**、**滚轮以光标为锚点缩放（1×–8×，逐帧缓动）**、
**放大后按住拖动平移**、`←/→` 在**当前可见集合**内翻图、点图片切"适应窗口 ↔ 真正 1:1"、
顶栏显示倍率（按原图像素算，100% = 1:1）、切图自动重置。

| # | 坑 | 根因与正解 |
|---|---|---|
| 1 | 放大后**拖不到最上/最左** | 容器用 `align-items/justify-content:center`，子元素比容器大时溢出部分被推到滚动起点之外。**正解**：容器只 `display:flex`，**居中交给子元素 `margin:auto`** |
| 2 | 缩放**第一下特别快**（像跳 1.5 倍） | 基准错用**原图像素**：适应窗口时显示宽度常只有原图 ~68%，`zf 1→1.06` 实为 1.06/0.68≈1.56 倍。**正解**：`onload` 量一次 **`fitW`**，`width = fitW × zf` |
| 3 | 滚轮缩放**顺带滚页面**、换设备步长差异巨大 | 必须 `{passive:false}` + `preventDefault()`；按位移算 `exp(-Δy×0.0006)` 且**归一化 `deltaMode`**（行×16、页×100） |

**安全**：`/gallery/img` 只服务白名单根目录内的图片后缀（`commonpath` 校验）；
**实测**三种目录穿越（`win.ini`、`README.md`、`..\..\`）**全部 404**。

## B8. 一张图串起"背后的调用原理"

```
点「改图」
 → client.js onClick → fetch POST /dsh-image-edit/ensure（同源，避开 /api 栅栏）
   → index.js：有 pidfile 则探活（node:http 短请求）；无则 spawn(detached) Python
     → mask_edit_app.py 起在 127.0.0.1:8000 并写 pid → ensure 等它应答 → 返回 {ok,url,pid}
 → 浏览器新标签打开 127.0.0.1:8000
    → 用户涂抹 → 表单 multipart POST /api/edit
      → 服务端把涂抹区**硬涂红**合成 image[1]，组装 image / image[1] / image[2…]
        → POST {base}/images/edits（Bearer key）→ 回 b64/url → 页面显示可下载

点「图片」
 → 同一个 ensure → 打开 /gallery
    → gallery_html() 只扫**最新一轮** → gallery_category() 打标签 → 服务端渲染卡片
    → 点图进灯箱（纯前端）；图片字节走 /gallery/img（白名单 + 后缀校验）
```

## B9. 未决与未验证（工程侧）

- 【部分已验证，2026-09-25】**两颗按钮是否已在 GUI 里出现**：**注册半边已实测通过**——
  插件自带的离线测试台 `dsh-plugins/_test_client_button.mjs` 全过，其中明确断言
  「BOTH entries registered with distinct ids: `image-edit`, `image-gallery`」，标签为「改图」「图片」。
  **但"宿主 GUI 真的把 `sidebar.footer.action` 渲染出来"仍未在浏览器里验证**：
  GUI 根页需 DSH 启动时打印的带令牌 URL（无授权直接 401），本机内置浏览器够不到那个会话。
  → 仍需你打开 GUI 刷新一次确认。
- 【未验证】`lib/client.js` 改动"刷新页面即生效"这一条从未在浏览器里验证过
  （`/plugins` 路由按 pathname+search **精确匹配预建响应表**，猜不到带 rev 的 URL；无授权的 GUI 根页面返回 401）。
  2026-09-25 复探同结论：`GET /plugins/dsh-image-edit/client.js` → **404**（与"精确匹配"一致，
  但**不能据此断定插件没加载**）、`GET /` → **401**。
- 【部分已验证，2026-09-25】**懒启动已实测跑通**：`dsh-plugins/_test_lazy_start.mjs` 全过——
  真的用 `C:\Python314\python.exe ...\mantu\compose\images\mask_edit_app.py --port 8000`
  把服务拉起（407 ms 就绪、pid 有值、`status` 转 running=true），二次 `ensure` 幂等不重复起进程，
  `/stop` 真的杀掉，卸载时 3 条路由全部移除。且它报出的 `script` 路径就是**搬迁后的当前路径**。
- 灯箱交互（滚轮/拖动/点空白）只做过**结构级断言**（`check_lightbox_close.py` 全过 + 页内 JS 过 `node --check`），
  **手感未在浏览器实测**（2026-09-25 仍未能验）
- 画廊 `scan_roots()` / `user_inputs()` 绑死本项目目录与会话约定，**不是通用功能**

## B10. 三次审查记录（针对本文档）

- **审查一（事实）**：所有行数/限值/体积数字都对齐本次命令输出与代码注释；
  把"客户端刷新即生效"从结论**降级为【未验证】**并写明为什么（响应表精确匹配 + 401）
- **审查二（可执行）**：补齐三处**必须改的本机路径**（`cordis.patch.yml` 的 `python/serverScript/serverCwd`）
  与三种**重启时机**（改 index.js 重启 DSH / 改服务只重启服务 / 改 client.js 刷新页面）；
  给出服务启动自检命令
- **审查三（安全/一致）**：确认插件包（`lib/`、`cordis.patch.yml`、`package.json`）与本文档**零密钥**；
  **明确警告** `mask_edit_app.py` 含 6 处硬编码密钥（含一处写进 HTML 的 `<input value="sk-…">`，
  会随页面发给访问者）→ **该文件不得随文档外发**。术语与文档 A 口径一致、不重复其内容只留指针。
  〔2026-09-25 补：上述警告已过时——**全仓库 39 处凭据已清除**，见 B11；**
  但 git 历史里仍是明文，对外发布仓库前须轮换 key**。〕

## B11. 2026-09-25 追加：凭据清除 与 插件两侧的离线验证

### B11.1 凭据清除（范围比 B10 审查三记的大 6 倍）

**实测范围**：全仓库用 `sk-[A-Za-z0-9]{20,}` 扫描，命中 **39 处、分布在 22 个文件**，
涉及 **2 个不同密钥值**（`sk-b692d…` 属 GPT 通道、`sk-4d824…` 属 Grok 通道）。其中 21 个文件被 git 跟踪。
文档原先只记了 `mask_edit_app.py` 的 6 处——那 6 处在该文件里是对的，但**不是全貌**。

**做法**：把所有凭据字面量删成空串（**字节级替换**，不动编码与换行）。选择"清空"而非"读环境变量"，
是因为 19 个受影响脚本里只有 3 个已在模块级 `import os`，补 16 处 import 的风险大于收益。

**语义确认**（清空前先读代码，不靠猜）：
- 服务端 `L2641` 只在**客户端未给 key** 时才回退用 `ENGINES` 里的 key，`L2646` 缺 key 会显式报错
  → 清空后走"页面必须手填"，**不会静默传空串**；
- 客户端 `L1699/L1764` 本来就要求 `base()` 与 `key()` 都非空才发请求。

**随动两处**（清空后不做就是引入退化）：
- `applyEngine()` 原来无条件 `$('apiKey').value = d.key`，默认值清空后会在**切换引擎时把用户手填的 key 清掉**
  → 改为 `if (d.key) ...` 守卫；
- 页面提示原文"key 已按你的中转站预填"在清空后成为假话 → 改为"key 需自己填写"。

**验证**【实测】：改动过的 `.py` 全部 `compile()` 通过；复扫 `sk-` 字面量 **0 处**；
服务启动自检（8011/8012 端口）`GET /` → 200、`GET /gallery` → 200，关停后端口释放；
**渲染页面实测 0 处凭据**，`apiKey` 输入框为 `value=""`。

**遗留风险（未处理，需你决定）**：这些凭据在 **git 历史里仍是明文**（本次未重写历史）。
清理工作树不等于清理历史，**真正的修复是去中转站作废并轮换这两个 key**。

### B11.2 插件两侧的离线验证【实测】

GUI 根页需 DSH 启动时打印的带令牌 URL（401），内置浏览器无该会话，故改用插件自带测试台：

| 测试台 | 结果 | 关键结论 |
|---|---|---|
| `dsh-plugins/_test_client_button.mjs` | **0 失败**（7 个场景） | 两颗按钮**以不同 id 都注册成功**；标签为「改图」「图片」；点击 → `POST /dsh-image-edit/ensure` → 打开路由返回的 URL（图片按钮为 `<url>/gallery`）；起不来时**不打开死页面**并显示原因 |
| `dsh-plugins/_test_lazy_start.mjs` | **0 失败**（8 组） | 注册 3 条路由；`ensure` **真的**拉起 Python 服务（407 ms 就绪）；二次 `ensure` 幂等；`/stop` 真杀掉；卸载时路由清空；坏配置不抛异常 |

附带收获：测试台打印的 `script` 是 `<工作区>\compose\images\mask_edit_app.py`，
即**搬迁后的当前路径**——这独立佐证了插件加载的配置已是新路径（原先担心的"需重启 `dsh web`"隐患不成立：
当前 DSH 进程启动于 2026-09-25 10:03，远晚于 09-18 搬迁与 09-20 profile 改动）。

**仍未验证的只剩宿主侧渲染**：DSH GUI 是否真的把 `sidebar.footer.action` 槽位画出来。
这一条必须用带令牌的 GUI URL 打开刷新才能确认，**不是插件侧的问题**。

### B11.3 宿主侧槽位的静态确认（2026-09-25 补）

顺着插件源码注释（它写明槽位由 `dsh-client-ui-sidebar` 渲染）追到了宿主包：

- `@deepseek-ai/dsh-client-ui-sidebar` **版本 0.1.2-rc.1，与 DSH 一致**（无版本错配）；
  其 `slots.d.ts` 声明 `'sidebar.footer.action': { kind: 'list', scope: 'root', … }`
  —— `kind: 'list'` 意味着**允许多个注册者**，所以放两个按钮合法；
  `lib/client.js` 里真的 `renderSlot("sidebar.footer.action", { wide })`，位置在
  `footArea > footerActions`（**侧栏底部**），owner props 恰为插件假设的 `{ wide }`。
- profile 里另装的 `dsh-better-sidebar` **没有抢这个槽位**——它自己的代码写着
  "`'sidebar'` 已被 DSH 自己的 ui-sidebar 占用，故另用不同名字"，它插入的是独立的一行、独立槽位名。
- 一个实用提醒：按钮住在侧栏底部，**侧栏折叠时这块可能看不到**，确认时先展开侧栏。

另有两条与"挂钩是否还活着"直接相关的事实【实测】：

- 插件的 `dsh.profile.bundles` **包含 `dsh-image-edit`**（光有 dependencies 不够，必须进这个清单才会被加载）；
- `profiles/web/node_modules/dsh-image-edit` 是一个**目录联结（junction）**，`readlink` 指向
  `\\?\<工作区>\dsh-plugins\dsh-image-edit`，5 个文件逐哈希相同——
  也就是说**改工作区里的插件文件就是改 DSH 会加载的那份**，不必往 profile 里复制。
  （注意 `os.path.islink` 对 Windows 联结返回 False，但 `ReparsePoint` 属性为真；
  只按 `islink` 判断会误判成"不存在"。）

## B12. 2026-09-25 追加：把改图能力搬进 ZCode（不再依赖 DSH）

用户口径：**要在 ZCode 里用这套改图能力，而非继续依靠 DSH**。交付为
**技能 + CLI + 斜杠命令**，凭据走环境变量，遮罩支持"代理生成 + 保留网页手涂"，
范围含改图 / 确定性本地操作 / 画廊。全部落在新目录 `zcode-image-edit/`（见其 README）。

### B12.1 复用边界：为什么这是换壳不是重写

【实测】`round_lib/run_round.py` 本身就可独立运行（`--help` 退出码 0），整条链路已具备：
体积预算 → 压缩梯子/JPEG → 发送（按返回值重试）→ URL 落盘 → 探测式抢档下载 → 完整解码校验。
遮罩合成的核心函数 `make_visual_mask / build_mask_alpha / normalise_to_size / build_multipart`
都是可导入的纯函数。因此 ZCode 侧**只补三样 DSH 曾经提供的东西**：

| 原由 DSH 提供 | ZCode 侧替代 |
|---|---|
| 鼠标刷子涂遮罩 | `zcode-image-edit/mask_gen.py` 的区域规格（矩形/多边形/漫水/前景分割）；网页手涂用 `zimage.py serve` 保留 |
| 侧栏按钮 + 懒启动 | `zimage.py serve / gallery / stop`（拉起、复用、按 pid 停） |
| 页面手填 Key | 环境变量 |

**从 DSH 侧剔除、ZCode 不需要的**：`sidebar.footer.action` 槽位注册、`ensure/status/stop` 三条路由、
`spawn(detached)` 懒启动、`/dsh-image-edit` 路由前缀、嵌入式 `PAGE` 的 localStorage 密钥流程。

### B12.2 凭据层（唯一的真缺口）

【文件核实】改造前：`gen.py` 的 `BASE` 硬编码、`KEY` 已被 B11.1 清成空串，
**全链路没有任何 `environ/getenv`**。现在改成：

```
RELAY_API_KEY    必填；为空时 require_config() 抛 SystemExit
RELAY_BASE_URL   选填，默认 https://image-direct.geiliapi.com/v1（自动去尾斜杠）
RELAY_MODEL      选填，默认 gpt-image-2
```

**为什么用 `SystemExit` 而不是返回错误码**：`run_round.post_with_retry` 用 `except Exception`
把异常映射成 `-1` 再重试 3 次，而缺凭据是配置错误、重试毫无意义。`SystemExit` 继承自
`BaseException`，不会被那个 `except` 吞掉，于是能带原因一路冒泡、直接终止。

同一层也接到了 `mask_edit_app.ENGINES` 的 `base`/`key`（服务端兜底），所以网页手涂
**不必再手填 key**。已实测断言：**环境变量里的 key 不会出现在渲染出的页面里**
（`GET /` 与 `/gallery` 都检不到，`apiKey` 输入框仍是 `value=""`）——网页手填的值只存浏览器
localStorage，服务端从不下发。

### B12.3 画廊「输入」页签的数据源替换

ZCode **不把用户上传的图存成独立文件**：它以 base64 data URL 存在
`<storage>/cli/artifacts/<净化后的 sessionId>/*.txt`，元数据在 `cli/db/db.sqlite` 的
`session_input.payload.attachments`（`{ref, fileName, mime, bytes, previewRef?}`，
**不含内容哈希**，只有传输期的 checksum 且不落库）。`ref` 是
`zcode-artifact://<sessionId>/<artifactId>`。

`zcode-image-edit/zcode_inputs.py` 按这条实测事实重建索引：过滤 `session.directory`
等于本工作区 → 取 `image/*` 附件 → 由 `ref` 定位 artifact 文件 → 去掉 `data:<mime>;base64,`
前缀解码 → **自己算 sha256** 与工作区图片配对。

**关键取舍**：输出的 schema 与 DSH 版**完全一致**（`paths` / `excluded` / `unreachable`），
因为画廊只消费这三个键；于是切数据源只需把 `GALLERY_USER_INPUTS` 指向新文件，
**不必动 `gallery_category` 的任何判定**——那条 64 条回归断言继续全过（本轮实测 64/64）。

顺带修掉一个**遗留崩溃**：`user_inputs()["atts"]` 是个死分支（`user_inputs()` 从不设置
`atts` 键，只提供 paths/excluded/unreachable），一旦被走到必抛 `KeyError`；改为 `.get` 后
优雅返回 404。

### B12.4 未验证（必须如实标注）

- 【未验证】**一次真实的改图调用**：要花额度、结果依赖上游。本轮的验证全部是本地确定性的
  （`--dry-run` 出预算报表、遮罩逐像素对账、服务自检、安装哈希自检），**首次真实调用留给用户决定**。
- 【未验证】画廊「输入」页签的端到端表现：结构已按实测的存储事实写好，但**本机 ZCode 还没有
  任何上传**——实测本项目 12 行 `sendText` 的 `attachments` 全为 `[]`、`input_history` 非空 0 行、
  artifacts 里 94 个文件全是工具结果 `.json`，**没有一个 `prompt-attachment-upload-*`**。
  所以当前只能验到"如实报 0"。在 ZCode 对话里真发一张图后重跑 `zcode_inputs.py` 即可。
- 【未验证】`--grabcut` 对动漫插画的效果未做对照，可能不如 `--flood` 稳。
- 【判断】技能与命令需**重启 ZCode 会话**才会被发现（ZCode 在会话启动时扫描），本轮未能重启验证。

## B13. 2026-09-25 追加：换上游 + 首次真实出图，查出 run_round 一个真缺陷

用户换到新中转站（`https://api-slb.micuapi.ai`，OpenAI 兼容）。为此做了一次**真实出图**——
这是本项目第一次真花额度，也正是它查出了前面所有本地验证都查不出的东西。

### B13.1 新中转站的实测画像

| 项 | 实测 |
|---|---|
| base 前缀 | **必须带 `/v1`**；裸域名那层拿不到 `GET /v1/models` |
| 可用模型 | 3 个：`gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst` |
| `/v1/images/edits` | **存在**。带 model 空载荷 POST → `500 "image is required"`（路由匹配、模型被接受、已走到校验图片） |
| `/v1/images/generations` | **存在**。带 model 无 prompt → `400 "Missing required parameter: 'prompt'"` |
| **内容审核** | **有**。实测被拦：`400 {"type":"moderation_blocked","message":"内容被安全审核拦截 (疑似成人内容)"}` |
| 结果尺寸 | 实测走 `b64` 路线，请求 1024×1536 回 **848×1264**（≈源图 837×1243）——与本文档既有的"**b64 路线不守 size**"记录一致 |

**内容审核是个新的运营约束**：旧上游 `image-direct.geiliapi.com` 没有这道闸，所以本项目历史里
"换背景/重画某人"这类配方从未考虑过它会拦。**实测那张穿白色吊带的人物图直接被拦**（`疑似成人内容`），
而纯背景图层顺利通过。→ 凡人像素材，做好被拦的准备；提示词与素材都要留退路。

### B13.2 run_round 缺遮罩说明（真缺陷，已修）

**症状**：`--mask --mask-primary` 走下来 HTTP 200，但**遮罩完全没生效**——模型把整张图重画了。

**根因**：`gen.py` 在遮罩路径下会前置 `mask_edit_app.SCHEMA_PROMPT`（那段 `[MASK INSTRUCTION]
… red areas are the ONLY places you may change …`），而 **`run_round.py` 从头到尾没用过 SCHEMA_PROMPT**。
于是发出去的只有用户那句"要改成什么"，模型看到一块红色却不知道红色是什么含义，就自行把整图重画了。

（这也解释了 A6 里那两次成功：**当时的提示词是手写的、自己带了"红色只是标记"那句**，
不是靠自动前置。这个隐含前提在把配方做成工具时被丢掉了。）

**修法**：`run_round.py` 在遮罩分支同样前置 `SCHEMA_PROMPT`（从 `mask_edit_app` 导入，与 `gen.py`
同一来源，避免两处文案日后漂移）；台账新增 `mask_prompt_prepended` 字段，便于回溯当时发出去的到底是什么。

**A/B 实测证据**（同一张图、同一个矩形 `200,400,640,840`、同一句提示词，只差这一处）：

| 指标 | 未前置 | 已前置 |
|---|---|---|
| 保护区 平均差异 | 20.71/255 | **1.49/255** |
| 保护区保持原样(<12) | 29.1% | **99.8%** |
| 可改区 平均差异 | 9.52/255 | 9.87/255 |
| 可改区 ÷ 保护区 差异比 | **0.46×（遮罩反向失效）** | **6.64×（遮罩生效）** |
| 结果体积 | 34 KB | 79 KB |

未前置那次，**保护区比可改区动得还多**（0.46×）——因为整图被重画，而重画后"可改区"恰好落在
渐变较平的位置，差异反而更小。这正是"只看 HTTP 200 就会误判成功"的活教材。

**教训**：本地验证（遮罩逐像素对账、预算、服务自检）**证明不了提示词有没有带对**。
这类"发出去的载荷形状"必须靠真实调用 + 事后定量对比才能发现。
