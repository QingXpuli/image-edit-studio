# 失败态负向词库

用法：第 5 段的"失败态"从这里取，并把本轮**新发现**的形态追加回来。
每条都必须是**可观察的形态**。禁止写"不要差""不要崩""不要很丑"这类空话。

---

## 一、手中物体与"捏"

```
hidden object held, object pinched between fingers, pinching air,
OK sign, thumb and index forming a circle, thumb and index pinched together
```
**历史依据**：把比心写成"围出小圆环"之后，连续两轮出来都是捏空气／OK 手势。
改法不是加形容词，而是把正向改成"交叉错开搭叠、上宽下尖、靠留白成形"，**同时**把 `pinching air`、`OK sign` 写进负向。

## 二、手部形态

```
fused fingers, extra fingers, missing fingers, clawed fingers,
long pointed fingernails, hard-angled knuckles, stiff rigid fingers,
clenched fists, hands clasped tight, fingers interlaced, hand-wringing
```
补充（防"该竖两根手指时只竖一根"）：
```
single finger raised only, second finger folded into the palm
```
正向配套写法：`两根手指都要竖起、彼此分开成一个小小的 V 形、两根手指都要清晰可见`。

## 三、肢体成一团（擦除后遗症专用）

```
limb rendered as one flat colour mass with no joints,
arm with no elbow or wrist, limb ending in a blunt stub,
single dark line running down the middle of the arm
```
正向配套写法：`按"肩线 → 上臂 → 手肘 → 前臂 → 手腕"的顺序画下来，各段要有明确的转折、宽窄变化与方向改变；肩与上臂之间要画出腋下的交界；手臂要有外轮廓与内侧轮廓这两条线`。
**并务必配合输入图预处理**：把造成这块色块的擦除痕迹中和掉（见 `erase_region.py --neutralize`）。

## 四、符号化 / 把负空间画成实心

```
solid filled heart drawn between the fingers, floating heart glyph,
hand-drawn glyph marks floating around the figure
```
正向配套写法强调"这个形状是**露出来的底色**，是一个看得见的空，不是画上去的实心色块"；且要把这条写进 STYLE 的 LIGHT 段（留白本身不加颜色、不描边）。

## 五、参考图身份泄漏

```
pointed elf ears, forehead stitch mark, blue teardrop earrings, blue thorn circlet,
off-shoulder dress, ribbon bow, frilled sleeves, ruffled cuffs,
high collar, round pendant, bracelet, star charm bracelet, wrist chain
```
**另加（2026-09-19 实测新增）**——参考图里的标志性小元素会以"变形后的位置"渗进来：
```
droplet accent under the eye, teardrop mark on the cheek, droplet mark anywhere on the face
```
依据：画法图的特征是"耳饰为蓝色水滴"，成图里出现了一枚**眼下的水滴**。
它既不是目标图的元素，也不是我们要的，属于同一母题换了位置。

**注意本条的写法**：第一版写的是 `blue droplet accent under the eye`——**按颜色拦**。
结果下一轮它换成了**白色**的水滴又出现了一次，颜色词失效。
**所以：按形状 + 母题拦，不要按颜色拦**（`droplet / teardrop` 不带颜色限定词）。
这条正是本词库自己的规矩，第一版却违反了它。
按实际情况取用；每轮要按 `identity-library.md` 的纪律做**逐项条件化**。

## 六、背景误伤（仅在"明确要换成纯白底"时才可加）

```
simple background, plain white background
```
**默认禁止**：保留目标图背景时写进这些词，会把目标图自己的背景一起抹掉。

---

## 七、道具被缩小／移位／浮空（风格重画时的高频漂移）

```
object shrunk, object moved to a different position, object floating off the head,
glasses floating above the head, accessory detached from the body
```
**历史依据**：彩色原图里"双手向上托着的大矩形物"改成漫画画法后，缩成一小块、挪到了嘴边；
头顶的眼镜变成浮在顶上的一圈空框。小道具既不像人物那样被反复点赞，也不像背景那样被结构绑定，
在风格重画里最容易被当"装饰"重新安排。
**正向配套写法**：写清道具**相对身体的位置与大致尺寸**——例如
`矩形物的下缘在她下巴以上、宽度超过肩宽、上缘被画面上缘裁切`，而不是只写"保留她手里的矩形物"。

## 八、姿态迁移的连带破坏：衣服被"改成合身的"、文字被新动作遮住

```
added suspenders, added belt, added shorts, added jacket, added outerwear,
garment redesigned to suit the pose, costume changed to match the action,
hand covering the caption, sleeve covering the caption, hair covering the subtitle area
```
**历史依据（两次不同轮次里都出现，形态在扩散）**：
① 动态全身姿态那一版，服装被重新设计——多出黑色背带、腰带与短裤（原图是白上衣＋黑领结＋蓝荷叶边）；
② 大头特写、手抬到嘴边那一版，抬起的手正好压住了下格字幕，字被手臂挡掉一部分；
③ **一页三视图那一版**：拦了"背带／腰带／短裤／外套"之后，它换了形态再来——
   多出**黑色短裙／背心裙与一排纽扣**，还多出**过膝长袜与玛丽珍鞋**（来自画法图那套女仆装）。
   → 说明"服装被改成合身的"这条**不能只列具体件名，要写成禁止整类**。
两条机理不同，但都属"**新动作没有和画面里既有的东西做碰撞检查**"。

**正向配套写法**：
- 服装的识别件要写成**与身体的关系**，而不是只列名字：
  `黑领结位于颈前正中、蓝色荷叶边在两侧肩部与袖口、白色上衣是主体`；
- 文字类元素要给出**位置带**：`字幕位于两格的中下部、画面中央的水平带内`。
- **服装要用"整类禁止"而不是"具名禁止"**：
  `不要新增任何外套／裙装／裤装／袜类／鞋类／背带／腰带／纽扣排；人物的服装只保留人物图里已有的那几件`。
  （具名禁止会被"换一个件名"绕过——背带被拦后它改成了短裙与长袜。）
- 更稳的做法：需要精细姿态时**一格一版分开生成**，避免两格同时改姿态相互牵制。

## 九、误读「通透」与拼接泄漏（2026-10-06）

```
sheer clothing, transparent fabric, skin visible through clothing,
glass skin, glowing rim light, plastic skin,
split-screen reference, text labels, watermark,
silver-blue hair, purple eyes, updo, extra character
```

「通透」只表示干净、透亮、简洁。衣服必须是能看清领口、袖口、腰和裙摆的实体布料。上下拼接不是双图输入，不得用它代替独立字段。

## 十、阴影灰调（2026-10-06 实测）

```
cold grey shading, grey midtones, desaturated shadows, muddy grey wash
```

**历史依据**：撑伞风格迁移 r9——STYLE 段写了「阴影是同色相的浅一档」，但没禁灰调，
结果阴影整体偏冷灰，画法变得厚重发闷。**正向「同色相加深一档」＋负向禁冷灰必须成对出现**，
只写正向压不住模型默认的灰调。

## 十一、细线乱线圈（2026-10-06 实测）

```
tangled hair coils, curly scribble loops, spring-like coiled strands,
knotted line tangles, scribbly tangled thin lines
```

**历史依据**：input_fidelity=low 路线在发丝/细线密集区的固有失败形态——发丝画成卷曲打结的乱线圈（放大裁片确认为螺旋碎线团，非「驳杂」的碎笔多，而是线的形状错误）。**正向「单向长曲线、平行成组」与负向本条要成对出现**；修复用 Lineart 定位＋避脸蒙版局部重绘（实测一次成功）。

## 十二、全图无结构碎斑（2026-10-07 实测）

```
speckled mottling, flaking paint patches, unstructured white blotches,
broken hair wisps, hooked stray strands, spiky firework burrs,
texture that is not a countable shape
```

**历史依据**：GPT i2i 祭典图（`产图/i2i-gpt-matsuri.png`）五区审查不合格。发丝是短碎线和回钩，扇面是灰白剥落，衣服是无结构白块，烟花边缘是毛刺。弱边缘 19.87%，画法图 14.20%。本地保边去噪只降到 18.46%；生成式修发改变了脸和构图。这和第十一节的乱线圈不是同一形态：乱线圈是线打结，碎斑是色块和纹理没有可数形状。

**正向配套**：发丝写成单向长曲线、平行成组；大色块内部变化少；扇面、布纹、烟花必须是可数的片、花或放射束，不是碎屑。两区以上出现本条时，停止局部修复，回到生成侧重做。

## 追加纪律

1. 每轮生成后，对照第 6 段体检清单，把**实际出现**的失败形态逐条加进来。
2. 一条失败态若已连续 3 轮未再出现，可以标注 `(稳定，可保留)` 但仍不删——它标记的是模型的固有倾向。
3. 同一处失败连续两轮未解决 → 不再加词，走"升级规则"（局部重绘／换通道）。两段式已停用，不得再作为升级路径。
