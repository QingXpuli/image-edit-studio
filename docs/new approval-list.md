# 待审批清单

本清单只记录需要主代理或用户明确批准的动作；本次不执行这些动作。

- 更新 `~/.agents/` runtime 安装副本，或覆盖其中与仓库不同的独有内容。
- 运行 `scripts/sync_skill.py` 的写入模式；当前只允许完整 manifest 与 installed-only 差异报告。
- 修改/禁用 global workflow，或把它从历史背景移除。
- 安装/更新插件、marketplace 或 `zcode-plugins` source catalog；插件 0.1.4 与 `C:/.../zcode-plugins/marketplace.json`、market `dev-mantu-images` 仅待主代理处理。
- 移动、归档、删除旧文档、旧脚本、runtime 记录或历史目录；本次只复制 history。
- 真实生成、API 请求、重试、模型 fallback、任何真实 key 写入。
- 将待实现的 `--task-file` / `review --job` 契约当作现有实现，或宣称 legacy direct scripts 已消失。
- 扩大用户 scope（身份/内容/配色迁移）、启用 `input_fidelity=low` 保身份、从认可基准全图重掷。

已完成的低风险动作：仓库内允许文档与技能精简、完整历史复制、LESSONS 短索引和静态校验准备。最终测试数字由主代理收尾填写。
