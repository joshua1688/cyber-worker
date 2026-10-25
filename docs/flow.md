# 流程引擎：录制、页面锚点、执行

最小闭环：**录制 → 标注页面锚点 → 空跑 → 正式执行 → 查看截图和审计**。

> 日常使用推荐 studio 网页编辑器（`uv run cyber-worker studio`，见 [studio.md](studio.md)），同一套流程都能点着完成。本文是命令行方式，也说明了流程文件格式。

## 前置条件

- macOS 或 Windows，已执行 `uv sync`
- macOS：「系统设置 → 隐私与安全性」里，给运行命令的终端授予**屏幕录制**、**辅助功能**、**输入监控**
- Windows：目标程序如果以管理员身份运行，终端也要以管理员身份运行，否则系统不允许向它发送键鼠
- 目标应用已登录（建议小号），窗口可以调整到你想要的尺寸
- 同一个程序开了多个窗口时，用 `--title` / 流程里的 `window.title` 指定标题里的一段文字

## 1. 录制

```bash
uv run cyber-worker record user_data/platforms/wechat/flows/moments_comment.yaml \
    --app WeChat --size 1200x900
```

窗口会被激活并调整到 1200×900。然后在窗口里正常操作；结束时点一下终端，按 `Ctrl+C`。

- 点到窗口外之后的按键不会被录进去（所以在终端按 Ctrl+C 是安全的）
- 连续打字合并成一个 `type` 步骤，组合键和回车单独成步
- 两次操作之间的间隔（≥ 0.3 秒）录成单独的 `wait` 步骤，可以直接改或删
- 坐标存的是**窗口内坐标**，同时记下缩放倍率

## 2. 编辑流程

录出来的 YAML 需要手工补三样东西（之后由 studio 完成）：

```yaml
name: moments_comment
window: {app: WeChat, width: 1200, height: 900, scale: 2.0}
variables: [comment_draft]          # ① 声明变量
steps:
- {id: s01, action: click, x: 540, y: 300}
- {id: s02, action: type, text: "{comment_draft}", expected_page: comment_box}   # ② 输入前必须在这个页面
- {id: s03, action: click, x: 1100, y: 820, sends: true, expected_page: comment_box}  # ③ 标记「发送」
```

规则：
- `type` 步骤和 `sends: true` 的步骤**必须**有 `expected_page`，否则执行时直接中止
- 页面还没出现时会等，最多 5 秒（每 0.3 秒重新截图核对），等不到才中止
- `sends: true` 的步骤在空跑时会被跳过，空跑到此结束

### 按图找（点击）

点击步骤可以带一张目标图。执行时先在整个窗口里找这块图，找到了点在它上面，点击处相对目标的位置和录制时一样；界面变了（列表多了一条、内容挪了）也能点中。窗口里有好几处一样的，选离录制位置最近的那个。找不到就等，最多 `timeout` 秒，超时中止这一步，**不会退回去点旧坐标**。studio 录制时会自动截目标图：点在有纹理、窗口里独一无二的地方（头像、按钮）才截，点在空白处、纯色输入框就保持固定坐标。

```yaml
- id: s01
  action: click
  x: 88                 # 录制时点的位置
  y: 172
  target:               # 目标图和它录制时所在的区域（点）
    image: 群聊.targets/s01.png
    x: 64
    y: 152
    w: 48
    h: 40
    timeout: 5          # 找不到时最多等几秒
    threshold: 0.8      # 匹配得分门槛
  use_target: false     # 可选：有目标图也按固定坐标点
```

### 等待页面

```yaml
- {id: s02, action: wait_page, page: 聊天窗口, timeout: 10}   # 等页面锚点对上再往下走，超时中止
```

比固定延迟靠谱：电脑卡的时候多等一会儿，页面出来了马上继续。

## 3. 标注页面锚点

让窗口停在目标页面（例如评论框已打开），截一块**这个页面独有、内容不会变**的区域（按钮、标题栏图标，别选好友头像和正文）：

```bash
uv run cyber-worker anchor user_data/platforms/wechat/flows/moments_comment.yaml \
    comment_box --region 1040,800,120,40
```

- 区域单位是**点**（窗口内坐标），不是像素
- 锚点图片和 `*.pages.yaml` 写在流程旁边，流程里的 `pages:` 会自动填上
- 同一个页面可以加多个锚点，**全部命中**才算在这个页面上

## 4. 空跑

```bash
uv run cyber-worker run user_data/platforms/wechat/flows/moments_comment.yaml \
    --dry-run --var comment_draft=拍得真好
```

会点开、核对页面、打字，但不会发送。打完的字留在输入框里，需要你手动清掉。

## 5. 正式执行

```bash
uv run cyber-worker run user_data/platforms/wechat/flows/moments_comment.yaml \
    --var comment_draft=拍得真好
```

- 急停：`⌃⌥⌘.`（`config.toml` 的 `executor.panic_hotkey`）
- 你在用电脑时：任务开始前和步骤之间会等你空闲 `resume_after_idle_sec` 秒；步骤进行到一半你动了键鼠，该步骤立即中止
- 结果：截图在 `user_data/screenshots/<日期>/`，审计在 `user_data/state.db` 的 `audit_log` 表（`suspicious = 1` 的是页面不符或出错的）

```bash
sqlite3 user_data/state.db "select step_id, status, page_score, detail from audit_log order by id desc limit 10"
```

## 退出码

| 退出码 | 含义 |
| :---: | :--- |
| 0 | 完成，或空跑正常停在「发送」前 |
| 1 | 页面不符中止 / 执行失败 / 被你的操作打断 |
| 2 | 参数、文件或环境错误（例如不在 macOS 上） |
| 130 | 急停 |
