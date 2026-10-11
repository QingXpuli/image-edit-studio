# 手涂改图画布（可选插件，源码 0.1.4）

仅在用户明确选择网页手涂时使用。图片生成委托生图项目的 `zcode-image-edit/bin/zimage.py`，共用任务、预检、指纹缓存、一次 POST、恢复与验收；没有另一套图片发送器。CLI、画布和本插件均不能把技术保存当作语义成功。

## 依赖和配置

插件解释器需安装 `requirements.txt`；生图项目解释器还需满足主 CLI 的依赖。插件 userConfig 中设置 `python`、`port` 和 `projectRoot`。`projectRoot` 必须是含 `zcode-image-edit/bin/zimage.py` 的项目根目录，通过 `ZIMAGE_ROOT` 传入服务端。未配置时源码工作区可按目录推导；已安装副本需要显式设置项目目录。

凭据推荐从 `IMAGE_EDIT_BASE_URL` / `IMAGE_EDIT_API_KEY` 或兼容环境变量读取。旧本机 `image-edit.local.json` / 插件 `local.json` 仅保留只读兼容，未自动迁移、清除或覆盖。页面保存默认值不再写凭据文件。凭据与地址成对使用；不下发服务端 Key，不把它写入任务、日志或命令参数。

## 能力与执行边界

- 保留手涂、矩形、橡皮、反选、遮罩导出，以及缩放与对比查看。
- 整图、局部和文生图均通过主 CLI；局部编辑不是物理像素保护保证，按保护区指标和语义检查验收。
- 一次编辑只使用一张身份图，其他图片是参考槽。明确请求多个采样时串行执行独立样本，失败即停，不自动补发。
- 输出格式转换在本地完成，不改变主结果的 PNG 哈希及验收绑定。
- 视频是原有实验性独立能力，本轮未重构、未调用、未验证真实模型；不属于图片主流程。
- 保留环回 Host/Origin、服务身份/PID 和外部图片代理安全检查。
- 只监听 `127.0.0.1`，不可暴露到公网。

## 本地测试

在使用虚假凭据、外网阻断和临时 Job/cache/ledger/pending 的隔离环境中运行：

```text
python server/_test_paint.py
python server/_test_security.py
python server/_test_mcp.py
python ../../zcode-image-edit/tests/test_web_contract.py
```

主项目的子进程回归另见 `zcode-image-edit/tests/`。安全和 fake relay 通过不证明真实上游、客户端安装、技能发现或真实语义成功。

## 手动更新与试用

本地测试市场根目录是 `zcode-plugins/`，清单为 `marketplace.json`，市场名 `dev-mantu-images`，插件 ID `dev-mantu-images/image-edit`，源码版本 `0.1.4`。

在 ZCode 的「插件市场 → 添加 → 添加插件市场」添加该根目录；已有此市场时刷新市场源，再在「个人」中更新插件。设置 `projectRoot` 后，新建任务选择该插件并输入「打开手涂改图画布」。预期只打开本机画布，不自动生图。是否安装、启用和试用成功需在客户端确认。

本轮只更新源码和清单，不自动注册市场、安装、更新、启用插件或发布。
