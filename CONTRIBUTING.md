# 贡献指南

## 当前入口

先读 [`docs/current-policy.md`](docs/current-policy.md) 和 `AGENTS.md`。迁移文档、旧 acceptance 数字和历史 relay 结论只用于追溯，不自动成为新 gate；本次默认不真实生成、不调用 API、不安装插件、不更新 runtime。

## 离网检查

主代理收尾时按实际工作区运行可用的静态检查，例如：

```powershell
python -m py_compile style-distill/_skill/style-distill/scripts/sync_skill.py
python style-distill/_skill/style-distill/scripts/sync_skill.py --check
```

`sync_skill.py` 的默认模式和 `--check` 只读，输出完整 source/runtime manifest、差异和 installed-only 项；只有明确审批后才使用 `--apply`。它不会删除 runtime-only 文件，覆盖前会先保留差异副本。

旧的可靠性、画廊分类或提示词 acceptance 数字不作为本次新门禁，除非主代理确认测试存在且与当前实现匹配。不得因为文档中的旧数字宣称当前 relay 或当前 CLI 已通过真实生成验证。

## 约定

1. 结论标注依据：`【实测】`、`【文件核实】`、`【判断】`、`【未验证】`。
2. 凭据只走环境变量；不写真实 key、个人路径或个人数据。
3. 先判断是否可用确定性本地操作；`prompt_only`、inspect、gallery、dry-run 不 POST。
4. 任务写清 `intent`、`input_roles`、`change_scope`、`deliverable`、`side_effect_policy`、`ui_policy`；身份、内容、配色 scope 不得擅自扩大。
5. 失败经验先作为候选，人工审核后才写入 references；不自动循环修改 skill。
6. 修改 Python 时，写回前后都用 `py_compile`/等价 compile 校验；本次文档精简不要求修改核心执行代码。

## 待实现接口

计划中的 `zimage-task-v1`、`--task-file` 和 `review --job --verdict pass|fail|uncertain --check x=pass` 是待实现契约，不应在文档或测试中冒充现有能力。统一 CLI generation、插件/marketplace、global workflow、归档/删除均需另行审批。
