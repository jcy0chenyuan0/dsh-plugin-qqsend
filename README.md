# dsh-plugin-qqsend（v1.2.0）

**发送** QQ 消息的 DSH 插件：向指定的**一个**群或好友发**一条**文本消息。
与只读的 `dsh-plugin-qqchat` **完全独立**（两个 bundle，可分别启停）。

> **为什么拆成两个插件**：`qqchat` 的全部价值建立在"它绝对不发消息"这个可信承诺上。
> 一旦同一个插件能发消息，那个承诺就没了。所以"读"和"写"分成两个包。

---

## 红线（写死在代码里）

- ❌ **不批量、不群发**：一次调用只发一条，没有"发给多个目标"的参数；
- ❌ **不自动回复**：不订阅消息、不做 bot 回路；
- ❌ **不做频率规避**：不随机延迟、不多号轮换、不绕风控；
- ❌ **不注入** QQ 进程、**不改** QQ 安装包；
- ❌ **不重实现** QQ 协议。

---

## 当前进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 | UI 自动化可行性探针 | ✅ 已跑：QQ 无障碍树为空，A 路线**暂不可用**（见计划书 §2.5） |
| P1 | 骨架 + 闸门 + mock + OneBot 通道 + 目标解析 + 4 个工具 | ✅ |
| P2 | 目标解析（群号/群名/好友） | ✅ |
| P3 | 真实发送（OneBot / NapCat） | ⏳ 待装 NapCat |
| P4 | 真实发送验收（需自备 NapCat / OneBot） | ⏳ |
| P5 | UI 自动化路线（A1′：以 `--force-renderer-accessibility` 启动 QQ） | ⏳ 往后放 |

计划全文为内部文档，未随仓库发布。

**已验证**（本地假 OneBot 服务器，不依赖任何第三方软件）：

- ✅ 9/9 端到端测试通过（真实 HTTP 请求路径、闸门、限速、白名单、审计全覆盖）；
- ✅ 目标解析：群号 / 群名都能解析到索引；
- ✅ dry-run 与缺 `confirm` 时**一个请求都不发**；
- ⚠️ 测试抓到并修掉一个真 bug：`--dry-run` 曾被闸门当成"干跑放行"从而**真的发出去**。

---

## 三道锁：怎么才会真的发出去

真实发送必须**同时**满足：

1. 配置 `safety.allowSend = true`（默认 `false`）；
2. 调用时 `confirm = true`（缺省只做 dry-run）；
3. 目标在 `safety.allowlist` 内（若配置了白名单）。

外加：限速（`maxPerMinute` / `maxPerTargetPerHour`）+ 审计日志（`~/.qqsend/sent.log`，不存完整正文）。

这样设计不阻碍正常使用（配好之后调用仍是一行），但把"误触发"和"变成群发器"从设计上排掉。

---

## 工具

| 工具 | 作用 |
| --- | --- |
| `qq_send_doctor` | 通道是否可用、**登录的是哪个 QQ 号**、总开关/白名单/限速/审计、目标索引 |
| `qq_send_targets` | 按群号/群名/好友号/好友名搜索目标 |
| `qq_login_qrcode` | **在 GUI 页面里显示登录二维码**（返回 Markdown 图片，聊天界面直接渲染成图） |
| `qq_download_media` | **把收到的图片/文件/视频/语音自动下载到系统「下载」目录**（增量、可轮询） |
| `qq_send` | 发消息：`to` + 正文，**并支持表情 / 贴图 / 连发 / 引用**（见下） |
| `qq_schedule` | **定时发送**：`add` / `list` / `remove` / `run`，到点自动发 |
| `qq_stickers` | **查可用表情包**：会话里最近出现过的，或本账号收藏的 |
| `qq_send_file` | **上传文件**到群/好友：本地路径或 URL，**由 DSH 决定传哪个文件** |
| `qq_send_history` | 查看发送审计记录 |

### qq_send 的四种附加能力

| 参数 | 说明 |
| --- | --- |
| `faces` | **系统表情（小黄脸）**，编号或名字：`["22","微笑"]`。QQ 不认的编号会被**丢弃并告知**——实测发一个不存在的编号会让整条消息被拒 |
| `stickers` | **贴图表情包**，传 URL；也可以传 `"latest"` 复用该会话最近出现过的表情包 |
| `messages` | **连发**：字符串数组，按顺序逐条发出，中间停 `delay_ms`（默认 900ms）。正文里写 `[换条]` 也能拆分 |
| `quote` | **引用**：传某条消息的 `message_id` |

> 实测要点：QQ 表情包**不是 `mface`**，而是 `image` 段 + `sub_type=1`；
> 同一个 URL 不带 `sub_type=1` 只会变成普通图片。这些都已写进代码和测试。

调用示例：

```
qq_send(to="<群名或群号>", text="hello，你好世界")                        # dry-run：只回显
qq_send(to="<群号或好友号>", text="hello，你好世界", confirm=true)              # 真发（需 allowSend=true）
qq_send(to="<群号或好友号>", text="看这个", faces=["13"])                       # 带个呲牙
qq_send(to="<群号或好友号>", stickers=["https://.../a.gif"], confirm=true)     # 发贴图表情包
qq_send(to="<群号或好友号>", messages=["在吗","问你个事","那个文件还在不"])      # 连发三条
qq_send(to="<群号或好友号>", text="收到", quote="123456789")                   # 引用回复

qq_stickers(source="recent", conversation="group:<群号>")              # 看看群里有哪些表情包
qq_send_file(to="<群号或好友号>", file=r"<文件绝对路径>")               # dry-run：只回显
qq_send_file(to="<群号或好友号>", file=r"<文件绝对路径>",
             name="实验报告.pdf", confirm=true)                           # 真传（需 allowSend=true）
qq_send_file(to="<群号或好友号>", file="https://example.com/a.zip")            # 也可以传 URL
qq_schedule(action="add", to="<群号或好友号>", text="记得喝水", every=60)       # 每小时提醒一次
qq_schedule(action="list")
qq_schedule(action="run", confirm=true)                                   # 把到点的真发出去
```

### 上传文件的边界

* **只传一个文件**，没有批量参数；
* 上传前做体检：**必须存在、必须是普通文件**（目录会被拒并说明原因），并回报文件名与大小；
  超过 100MB 给提醒但不阻止；
* 真实上传与发消息共用同一套闸门（allowSend / 白名单 / 限速 / 审计）；
* 审计**只记文件名与大小，不记本机完整路径**——日志不该成为目录结构的泄露面；
* 群文件可用 `folder` 指定目录 id。

### 定时发送的边界（说实话）

本插件**自身不带常驻进程**。`qq_schedule` 负责"记账"，但`run` 需要被调用才会去检查到点任务。
要真正无人值守，请把下面这条挂到你自己的计划任务里（Windows 任务计划 / cron 都行）：

```
python -m qqsend schedule run --confirm
```

每次执行**照样过发送闸门**（allowSend / 白名单 / 限速 / 审计）——定时任务不是绕过安全的后门。


---

## 安装与配置

> **首次安装**：先在本目录运行 `install.ps1`（Windows）或 `node install.mjs`（macOS/Linux）。
> 脚本按当前目录自动生成 `cordis.patch.yml`（已 `.gitignore`），不写死任何盘符。

1. 安装 bundle：先在本目录运行 `install.ps1`（Windows）或 `node install.mjs`（macOS/Linux），再在 GUI「设置 → 插件 → 安装 → 本地路径」填本目录的绝对路径；
2. 复制 `config.example.json` 为 `config.json`，按需修改；
3. **重启 DSH**（改代码不热生效）。

`config.json` 关键字段：

```json
{
  "transport": "onebot",
  "safety": { "allowSend": false, "maxPerMinute": 5, "maxPerTargetPerHour": 20, "allowlist": [] },
  "onebot": { "baseUrl": "http://127.0.0.1:3000", "token": "" },
  "qqchat": { "decryptedDir": null }
}
```

---

## OneBot 通道（当前默认）

需要 **NapCat**（或 LLOneBot）在本机提供 OneBot 11 HTTP 接口。本插件只调用两个接口：

- `get_login_info`（体检，确认**是哪个号在发**）
- `send_group_msg` / `send_private_msg`（发一条）

**安全约束（代码强制）**：

- **只接受 `127.0.0.1` / `localhost`**。NapCat 默认绑 `0.0.0.0` —— 这正是 2025-09-05
  公网未授权 OneBot 被利用群发、涉事账号与群**全部永久封禁**的根因；
- 配了 token 就一定带 `Authorization: Bearer`。

> ⚠️ **封号风险如实告知**：用机器人框架登录 QQ 属于自动化使用，与腾讯用户协议相抵触，
> 存在账号被风控甚至永久封禁的现实风险。**建议用不常用的小号做发送端，别拿主号试。**
> 发送账号由 NapCat 里登录的那个号决定；同一个号通常不能同时登在两个 PC 端。

---

## 零依赖

- **JS 半边**：只用 Node 内置模块；
- **引擎**：只用 Python 标准库（`urllib` 发 HTTP，无第三方 HTTP 库）；
- 不需要装 Python 包、不需要 `requests`。

---

## 目录

```
qqsend/
├─ package.json / cordis.patch.yml.tpl / config.example.json / README.md
├─ lib/index.js               宿主半边：4 个工具（零依赖桥）
├─ engine/qqsend/
│  ├─ cli.py                  doctor / targets / send / history
│  ├─ guard.py                发送闸门：开关 / 确认 / 限速 / 审计 / 白名单
│  ├─ targets.py              目标解析（软依赖 qqchat 的解密库）
│  ├─ paths.py config.py
│  └─ transports/             base / onebot / mock
└─ engine/tests/              9 项端到端测试（含本地假 OneBot 服务端）
```

---

## 自动下载图片 / 文件到「下载」目录

`qq_download_media` 会把消息里收到的**图片 / 文件 / 视频 / 语音**自动存到**系统自带的「下载」文件夹**。

```
qq_download_media()                             # 扫一次，下载新收到的媒体
qq_download_media(dry_run=true)                 # 只看会发现什么，不下载
qq_download_media(duration_seconds=300)         # 轮询 5 分钟，一有新图片/文件就下载
qq_download_media(kinds="file", dest="<下载目录>")  # 只要文件，并指定别的目录
```

**下载目录是动态解析的**，不是写死的路径：

1. 先走 Windows 已知文件夹 API `SHGetKnownFolderPath(FOLDERID_Downloads)` —— 这是"系统自带的"标准做法，
   你把「下载」改到别的盘、或 OneDrive 接管，它都会返回**当前真实位置**；
2. 老系统退回注册表 `Shell Folders`；
3. 再退回 `%USERPROFILE%\Downloads`。

`qq_send_doctor` 里会显示当前解析到的下载目录**以及它是怎么定出来的**。

其他特性：

- **增量**：游标存在 `~/.qqsend/media-cursor.json`，重复调用不会重复下载同一张图；
- **两条来源**：有直链就走 HTTP 下载；没有就问 NapCat 要**本地路径再复制**（NapCat 就在本机，复制更快）；
- **拿不到来源的会如实列出**（连着类型和文件名），不会静默丢弃；
- 文件名保留原始名字，重名自动加 `_1`、`_2` 后缀；非法字符替换成 `_`。

---

## 可移植性 / 换机器安装

这个仓库**不包含任何本机绝对路径、QQ 号或 OneBot Token**：`cordis.patch.yml` 由安装脚本按当前目录生成（已 `.gitignore`），`config.json`、`data/`、`*.log` 都不入库。

换一台机器：

1. `git clone` 本仓库到任意目录；
2. 在仓库目录运行 `powershell -ExecutionPolicy Bypass -File .\install.ps1`（Windows）或 `node install.mjs`（macOS/Linux）——生成 `cordis.patch.yml`，并准备一份 `config.json`；
3. 在 DSH 里执行 `plugin_manager install_bundle  file:<本目录的绝对路径>`；
4. 修改 `config.json`：填 OneBot 地址与 Token，**确认 `safety.allowSend` 仍为 `false`**，需要真发时再显式打开。

### 运行环境

- **Windows 10/11（x64）**：下载目录走 `SHGetKnownFolderPath`，NapCat 路径按 Windows 处理。
- **Python 3.11+**：优先使用 DSH 自带运行时；引擎只用标准库。
- **NapCat / OneBot**：需要另装并登录，本插件不带协议实现。
- **macOS / Linux 未适配**：OneBot HTTP 部分理论可用，但本地路径与下载目录逻辑需要移植。
- 默认 dry-run；真实发送要同时满足总开关、白名单与限速。
