# 改图画布：ZCode MCP 可选插件

提供手涂/矩形遮罩、图生图与文生图面板、结果缩放和对比查看器，以及实验性视频入口。该插件是独立的本机网页组件，不修改现有 CLI、`round_lib` 或 DSH 插件。

**默认仍使用仓库的 AI/CLI 流程。** 仅在用户明确要求手工绘制区域或打开画布时调用本插件；安装后不会替代 `image-edit` 技能和 `/image-edit` 命令。

## 依赖与启动

使用 Python 3.10+。Pillow 用于图像处理，MCP Python SDK 用于 stdio 工具服务；两者均必须安装到插件实际使用的解释器：

```sh
python -m pip install -r requirements.txt
python server/mask_edit_app.py --port 8000
```

直接启动后访问 <http://127.0.0.1:8000/>。ZCode 插件配置中可设置 `python` 为解释器或虚拟环境的完整路径，默认 `python`；`port` 默认为 8000。

只监听 `127.0.0.1`，不要通过 `0.0.0.0`、反向代理或隧道暴露到公网。

## ZCode 组件

- 插件标识仍为 `image-edit`，版本 `0.1.3`。
- 画布技能名为 `image-edit-canvas`，命令为 `/image-edit-canvas`，避免遮蔽原 CLI 技能和命令。
- MCP 工具：`image_edit_status`、`image_edit_ensure`、`image_edit_open`、`image_edit_stop`。
- manifest 仅引用 `.mcp.json`，不重复维护服务定义。
- `${CLAUDE_PLUGIN_ROOT}` 与 `${user_config.python}` / `${user_config.port}` 是 ZCode 支持的插件运行时变量；不需要手写安装缓存路径。

通过 ZCode 的插件管理界面添加/安装此源码后，在插件配置中选择正确的解释器。测试提示词：“打开手工改图画布”。预期调用 `image_edit_open` 返回本机 URL，用户在画布里绘制区域。是否在某个 ZCode 版本中已经加载和连接，需在客户端确认；文件存在不代表已安装或启用。

## 接口与凭据

推荐环境变量：`IMAGE_EDIT_BASE_URL`、`IMAGE_EDIT_API_KEY`。也兼容 `OPENAI_BASE_URL` / `OPENAI_API_KEY`，历史别名 `GEILI_SUB2API_KEY`。

可选的本机配置文件为 `~/.zcode/image-edit.local.json`：

```json
{
  "base_url": "https://your-relay.example.com/v1",
  "api_key": "YOUR_API_KEY",
  "model": "gpt-image-2"
}
```

也读取插件根目录未跟踪的 `local.json`。这些文件存储**明文**凭据，只能放在本人有权限的机器上，不能提交、分享或放进插件包。页面“保存为本机默认”是显式的本地保存操作。

`/api/defaults` 仅返回 `has_key` 布尔值，不返回 Key。服务端已配置凭据时，页面 Key 留空即可；Base URL 必须与配置相同。更换中转地址后需显式填写对应凭据，不能自动把原站点 Key 发给新站点。手动填入页面的 Key 会存在浏览器 localStorage；使用共享机器时应清除站点数据。

保存相同 Base URL 且 Key 留空时会保留已有本机文件中的 Key，不把环境变量 Key 复制到文件；切换 Base URL 不保留旧 Key。

## 功能与限制

- **画布**：画笔、矩形对象选中/移动/Delete、橡皮、反选、全填、复制遮罩。内容图和参考图支持拖拽、文件选择及 Ctrl+V 粘贴。
- **结果**：缩略图、单张缩放平移、按住对比、并排和滑动对比。
- **图片生成**：清晰度、比例、数量与 PNG/JPEG/WebP。价格仅为前端示例估算，应按实际中转计费修订；不是费用保证。不同上游可能拒绝页面提供的尺寸或模型。
- **局部改图**：只发送涂红图和参考图，不发送干净原图；提示词说明红色只是编辑标记。此方法源于特定中转站实测，不保证所有模型都遵守；也不提供物理像素保护或主 CLI 的保护区量化验收。
- **视频（实验性）**：先提交 `/videos`、轮询、下载。仅遇到明确的 404/405 才回退 `/videos/generations`。真实模型/计费未在安全回归中测试。

POST 生成不自动重试：超时、断连或 5xx 可能意味着上游已受理。若受理状态未知，应先核实账单/任务结果，不能根据“没有图片”断言未扣费。GET 查询可有限重试，下载失败不会重新提交生成。

## 安全边界

- 每个入口检查环回 Host；带 Origin 的请求必须同源，拒绝跨站和 `Origin: null`。
- 外部图片代理和上游结果 URL 每一跳只允许公网 HTTP(S)，连接使用已经校验的 IP；不将中转站 Authorization 转发给 CDN。内网图片 URL 被拒绝时，请保存到本机后上传。
- 带 Authorization 的中转请求不跟随跨源重定向。
- 默认配置响应不下发服务端 Key；MCP 返回的是状态与本机路径，不是凭据。
- MCP 停止服务只针对健康检查身份和 PID 都匹配的服务，未确认归属时不终止进程。

以上不能防止同机恶意进程或被恶意修改的页面；不要把服务暴露到其他设备。这个可选网页入口尚未统一接入主 CLI 的 Job/manifest/cache/语义验收，不应据此宣称完整 AI 流程已通过。

## 本地回归

在安装同一 `requirements.txt` 的解释器中运行：

```sh
python server/_test_paint.py
python server/_test_security.py
python server/_test_mcp.py
```

安全测试使用虚假凭据、确定性网络桩与动态端口 loopback 服务，不访问真实中转站、不消耗额度。MCP 测试执行 stdio 初始化、列出工具和状态查询；不自动打开浏览器。客户端安装和真实生成仍需单独验证。
