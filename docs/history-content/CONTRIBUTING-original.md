# 贡献指南

## 开始之前

- 提交 PR 前先跑本地门禁（均不联网、不消耗额度）：

```powershell
python zcode-image-edit/tests/test_reliability.py      # 期望: 0 failures
python style-distill/round_lib/lint_prompt.py --all-rounds   # 期望: 0 条未处置 FAIL
python style-distill/assert_gallery_categories.py      # 期望: 64/64
```

- 改动任何 `.py`：执行前 `python -m py_compile <文件>`，写回后再全量编译复核一次。

## 约定

1. **结论标注依据**：【实测】（真的跑出来）/【文件核实】（读代码得出）/【判断】（推断）/【未验证】。
2. **能出数就不靠肉眼**：面积用 %、体积用 MiB、结果用完整解码校验；新增验收逻辑必须带可量化指标。
3. **凭据只走环境变量**（`RELAY_API_KEY` / `RELAY_BASE_URL` / `RELAY_MODEL`），任何 PR 引入硬编码凭据会被拒绝。
4. **一轮一个变量**：调提示词/参数时一次只改一处，否则失败无法归因。
5. **失败态要沉淀**：发现新的生成失败形态，请往 `style-distill/_skill/style-distill/references/negative-library.md` 追加可观察的形态描述（禁止“不要崩”这类空话）。
6. 提交信息用一行中文说清“做了什么 + 为什么”，参考现有 git log 风格。

## 不接受的内容

- 硬编码的个人路径（`C:\Users\...`）——用 `__file__` 推导、环境变量或占位符；
- 真实图片/个人数据/凭据样本；
- 绕过预检、验收或原子保存的“快速路径”。
