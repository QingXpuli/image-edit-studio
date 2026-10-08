---
description: 打开即梦式无限画布：放图、加字、写提示词、生成结果贴回画布
argument-hint: "[可选：端口]"
skills: image-edit
---

用户要打开无限画布（对照多图、加注释、在画布上生成）。可选参数（端口）：

$ARGUMENTS

执行：

`C:\Python314\python.exe zcode-image-edit\bin\zimage.py board`

画布在 `http://127.0.0.1:8000/board`。生成仍走现有 `zimage.py edit` / `run_round.py`：一次 POST、指纹缓存、环境变量凭据。先点「先看预算」再确认生成。不要把 API Key 填进页面。
