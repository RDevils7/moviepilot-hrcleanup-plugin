# MoviePilot V3 插件：H&R 做种清理（HrCleanup）

> 订阅下载 → 媒体整理复制到 115 网盘 Plus → 做种达标且超过 H&R+8 小时 → 自动删除
> **种子任务 + 下载历史条目 + 硬盘原始文件**，**115 网盘上的副本永久保留**。

---

## 它解决什么问题

MoviePilot 订阅下载的文件落在**本地硬盘**，媒体整理会**复制（copy）**一份到 115 网盘媒体库。
副本进 115 后，硬盘上的原始文件、下载器里的种子、下载历史条目都是冗余——但 PT 站有 **H&R 考核**，做种不够会封号。

MoviePilot 内置的"整理后清理"只在 `move` 模式生效，**复制模式永不触发**，也没有 H&R 判定，所以需要本插件。

本插件在**四道闸门全部通过**后，按安全顺序自动清理，并且**对 115 只查询、从不删除**。

---

## 判定逻辑（四闸门，全部满足才动）

| 闸门 | 含义 | 不满足时 |
|---|---|---|
| **G1** | 存在成功的整理历史：`download_hash` 对应、目标存储为 115（自动识别 `u115` / `115网盘Plus` 等含「115」的存储）、`status == True` | 跳过 |
| **G2** | 复制成功确认：以「媒体整理记录成功」为准（默认**不再**按原路径复核云端——115 会把副本转移到正式目录，原路径届时已不存在） | 跳过 |
| **G3** | 做种时长 `seeding_time >= max(36, H&R+8) 小时` | 跳过，下一轮再试 |
| **G4** | 每条源文件都在**下载目录**内，且不在**媒体库目录**内 | 跳过并告警 |

> G3 的做种时长**以下载器（qBittorrent / Transmission 等）报告的原生做种时长为准**，比按下载历史时间估算更准确。
> qBittorrent 取 `seeding_time`，Transmission 取 `secondsSeeding`；读不到时长时**保守跳过**，不会误删。
>
> **种子已不在下载器**（例如被其它插件删掉）时由 `missing_torrent_policy` 决定：
> `assume_done`（默认）= 任务已消失即已停止做种，视为做种义务结束、允许清理（避免文件永远残留）；
> `skip` = 无法校验就跳过。若下载器**连不上**（查询失败），一律按「状态未知」跳过，不会误删。
>
> 阈值示例：H&R=24h、额外 8h、做种下限 36h → `max(36, 24+8) = 36 小时`。H&R 时长可在配置页随时调整。

## 执行顺序（不可颠倒）

1. **S1 删种子（同时删文件）**：`delete_torrents(delete_file=True)` —— 等价 qBittorrent 的「同时删除已下载的文件」，
   由下载器自己删硬盘文件，最可靠。删除后可再开启「插件删本地文件补刀」以确保删除成功。
2. **S2 校验/删除本地文件**：S1 后若硬盘仍有残留，用 `StorageChain().delete_file(...)` 兜底删除
   （目录走 `delete_media_file`，带根目录/媒体库目录边界保护）。
3. **S3 删下载历史**：`DownloadHistoryOper().delete_history(id)` + 文件标记 `state=0`

> `delete_with_files` 开关控制 S1 是否让下载器连带删文件（默认 True）。
> 关闭后 S1 只摘任务（`delete_file=False`），文件全部由插件在 S2 删除。

**永不执行**：删除 115 上的任何文件。整理历史默认保留（可配置一起删）。

---

## 安装

### 方式一：作为插件市场仓库（推荐）
把本目录（含 `package.v3.json` 与 `plugins.v3/`）推送到 GitHub，然后在
MoviePilot →「设置 → 插件市场 → 添加插件仓库」填入仓库地址，安装「H&R 做种清理」并重启。

### 方式二：手动放置
将 `plugins.v3/hrcleanup/` 整个目录放到 MoviePilot 的插件加载目录（Docker 部署常见为映射到容器内的
`/config/plugins`，具体以你的部署为准），重启 MoviePilot，在「插件」页找到「H&R 做种清理」并启用。

> 依赖：无需额外 pip 包，全部使用宿主已有库（apscheduler 等）。

---

## 首次使用（务必先演练）

1. 打开插件配置，**保持「演练模式（dry_run）」开启**，确认「只处理订阅来源」开启。
2. 按你的站点填写 **H&R 要求时长（小时）**（可填小数，如 24、36、72），做种下限默认 36，额外等待默认 8。
3. 保存后点插件页的「立即扫描」（或访问 `/api/v1/plugin/HrCleanup/run`）。
4. 查看插件**数据页**：「删除记录」表格逐条列出时间/标题/下载器/做种时长/阈值/文件数；「最近执行记录」写明跳过原因。
5. 观察 1~2 天，确认命中的都是该删的、且没有被误判，再**关闭 dry_run** 正式启用。

---

## 配置项

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | false | 总开关 |
| `dry_run` | true | 演练模式，只记录不删除 |
| `cron` | `0 4 * * *` | 定时扫描（每天 04:00） |
| `min_seed_hours` | 36 | 做种时长硬下限（小时，可填小数） |
| `hr_hours` | 24 | 站点 H&R 要求时长（小时，**可调整**，可填小数） |
| `extra_hours` | 8 | H&R 之后再等待的小时数 |
| `dest_storage` | 空（自动识别） | 认定"已复制到"的目标存储类型；留空自动识别含「115」的存储（`u115` / `115网盘Plus`） |
| `only_subscribed` | true | 只处理订阅来源 |
| `max_per_run` | 5 | 单轮最多清理任务数 |
| `scan_days` | 30 | 回看下载历史天数 |
| `verify_remote` | false | 是否按原路径复核云端（**默认关**：115 会把副本转移到正式目录） |
| `verify_size` | true | 若开启 `verify_remote`，是否再比对大小 |
| `delete_with_files` | true | 删种时让下载器「同时删除已下载的文件」（最可靠）；关闭则只摘任务、由插件删本地文件 |
| `missing_torrent_policy` | assume_done | 种子已不在下载器时：`assume_done` 视为达标允许清理 / `skip` 保守跳过（下载器连不上时一律跳过） |
| `delete_transfer_history` | false | 是否连整理历史一起删 |
| `extra_allow_paths` | 空 | 额外允许删除的路径（每行一个） |
| `notify` | true | 是否发通知 |
| `notify_only_on_action` | true | 仅有删除时通知 |
| `retry_cooldown_hours` | 24 | 失败任务冷却时间 |

## API

- `GET /api/v1/plugin/HrCleanup/run` —— 立即扫描并按当前 `dry_run` 执行；可带 `?hash=<种子hash>` 只处理单个
- `GET /api/v1/plugin/HrCleanup/preview` —— 只判定不执行（强制演练）

> 实际前缀以 MoviePilot 版本为准，插件页按钮内部会自动调用。

---

## 安全提示

- 默认 **dry_run 开启**，第一次务必先观察。
- 115 全程只读：只调用查询接口，从不删除云端文件。
- 云端查询失败（限流/鉴权/网络）一律视为"状态未知"→ **跳过，不删**。
- 只删「下载目录」内的文件；媒体库目录内的文件一律拒绝。
- 建议 MoviePilot 的下载历史自动清理天数（`DATA_CLEANUP`）大于本插件阈值天数，否则条目被提前清掉会导致无法判定。

---

## 目录结构

```
moviepilot-plugin-hrcleanup/
├── package.v3.json                 # 插件元数据（类名 HrCleanup）
├── README.md                       # 本文件
└── plugins.v3/
    └── hrcleanup/
        ├── __init__.py             # 插件主类（四闸门 + 三步清理 + UI/API）
        ├── requirements.txt        # 无额外依赖
        └── README.md
```
