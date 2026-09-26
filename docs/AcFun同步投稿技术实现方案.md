# yt2bili AcFun 同步投稿技术实现方案

版本：v1.2 实现与验证记录

日期：2026-09-26

状态：已实现本机实验性适配与本地模拟验证；网页会话投稿尚未在真实 AcFun 账号验证。
需求文档：[AcFun 同步投稿 PRD](AcFun同步投稿PRD.md)。

## 实施现状（2026-09-26）

代码已接入 `yt2bili/acfun.py`、数据库 v5、`acfun_accounts`/`acfun_attempts`、AcFun lane、桌面 RPC、账号与新建任务界面、CLI 和模拟测试。实验开关默认关闭，扫码 Cookie 留在系统凭据库；最终 `createDouga` 发送前先记录意图，只有有效 `dougaId` 记为已提交。`createVideo` 或最终创建响应不确定时保留素材并转待核对；确认未提交后才允许手动继续。当前仅支持预览后确认，自动模式被拒绝。

本地验证：AcFun 单元/调度 9 项、旧迁移与导入相关 6 项、抖音 v3 兼容用例、TypeScript、Vite 构建、AcFun 与抖音 Playwright 共 3 项通过。较广的 Python 回归执行 167 项，最终仅旧 v3 人工模拟迁移用例失败；该用例修复后单独复测通过。抖音 broker 的加密测试缺少可选 `cryptography`，未获得通过结论。真实扫码、上传及 AcFun 作品回执仍待授权账号验证。

与原方案的当前差异：分区目前由用户填写 ID，尚未接入经过验证的在线分区列表；平台作品查询接口尚未实现，因此响应丢失仅支持人工核对；网页接口条款及当前字段限制尚未得到平台确认。后续正式开放需补齐这些门槛。

## 1. 可行性判断与接入门槛

现有代码已经具备任务级素材准备、`task_publications` 分平台结果、Bilibili 每账号队列及抖音独立队列，因此增加 AcFun **内部结构上可行**。[Y2A-Auto 的 AcFun 登录与上传源码](https://github.com/fqscfqj/Y2A-Auto/tree/main/modules) 又提供了可研究的网页会话投稿链路：扫码取得 Cookie、向创作中心申请视频上传令牌、分片传输、创建视频素材、上传封面、创建稿件并读取 AC 号。这提高了技术可行性的可信度；它不证明该网页接口对第三方开放、当前稳定或已经过本项目真实账号验证。

首期采用**本机 Web 会话适配器**，不要求部署抖音式 broker。以用户主动扫码建立会话，使用严格限定域名的创作中心接口；界面明确标识实验性接入，功能开关默认关闭，用户主动启用后才可在预览模式选择 AcFun。扫码成功只把认证状态变为“已登录”，不自动启用投稿。条款及账号风险核对、真实投稿与故障验证是正式开放门槛。获得 AcFun 正式第三方接口时优先换成正式适配器，不改变任务/队列数据模型。此设计不包含浏览器模拟点击或要求用户交出账号密码。

### 1.1 Y2A-Auto 的实际链路与不能照搬的部分

| 阶段 | 源码观察 | 本项目处理 |
|---|---|---|
| 登录 | `acfun_auth.py` 调用扫码开始、查询扫码、查询手机确认，成功后把会话 Cookie 写成 JSON；`acfun_uploader.py` 也支持 Cookie 文件和账号密码登录 | 仅提供用户主动扫码；Cookie 放系统凭据库或受保护的本地凭据文件，不提供密码登录默认路径。账号身份再独立核实 |
| 视频上传 | `getKSCloudToken` 返回远端 `taskId/token/partSize`；按服务端块大小发往快手上传域名，调用 `complete`；再经 `createVideo` 得到视频素材 ID | 分片大小来自响应，记录远端上传任务 ID 与本地 `task_id` 的区别；每步核验业务码和可恢复证据 |
| 封面 | 获取封面上传令牌、上传文件，再获取封面 URL | 使用任务私有临时文件，确认上传及 URL 都成功；不让同名临时封面在并发任务间碰撞 |
| 最终投稿 | `createDouga` 发送标题、简介、标签、分区、原创/转载、来源、封面、视频素材；返回 `dougaId` | `dougaId` 才是提交回执；发送前持久化意图，响应丢失转待核对，禁止盲重发 |

参考源码的 `test_login()` 在某些非 JSON、非登录页 HTML 响应下也会返回成功；`complete_upload()` 和 `upload_finish()` 的失败路径可能只记录日志而继续；标题/标签超长时会静默截断。这些做法不适合作为本项目的成功判据或用户内容处理策略。[扫码登录](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_auth.py)、[上传器](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_uploader.py)

## 2. 当前代码接入点与硬编码

| 位置 | 当前行为 | 实施改动 |
|---|---|---|
| `yt2bili/db.py` | schema v4；任务以 `task_id` 隔离，父任务绑定一个 Bilibili 账号 | 新增明确的 v4→v5 迁移；父任务继续持有素材，不复制父任务 |
| `yt2bili/publications.py` | 平台 CHECK 只接受 `bilibili/douyin`；`dual()` 以存在抖音判断多目标；`account_label` 二选一 | 支持 `acfun`，增加 `multi_target()`，用平台注册表解析账号标签与目标状态 |
| `yt2bili/scheduler.py` | `douyin_lane` 单列；`fanout()` 非抖音一律进入 Bilibili；快照、唤醒和退出遍历写死 | 注册 `acfun_lane`，按 `(platform, account_id)` 路由；所有目标共用父工作锁、各持取消事件 |
| `yt2bili/desktop_service.py` | `tasks.create` 仅有 `sync_douyin`；抖音创建/去重路径写死 | 增加 AcFun 目标的前置校验和同一事务创建；保留旧请求兼容 |
| `yt2bili/douyin.py` / `douyin_broker.py` | 抖音特定 OAuth、接口与单账号服务 | 不复用抖音 OAuth 或 broker；仅借鉴状态和投稿台账边界，AcFun 本机适配独立 |
| `desktop/src/App.tsx` / `Douyin.tsx` / `types.ts` | 新建弹窗只有抖音开关，Publication 平台联合类型只含两个值 | 增 AcFun 状态、开关、账号卡片、元数据编辑与分平台进度 |
| `yt2bili/desktop_settings.py`、CLI、`desktop/src/preview.ts` | 抖音设置和模拟状态专用 | 加 AcFun 实验性接入开关、CLI 选项与前端预览模拟 |

当前 `publications.project()` 已依据所有目标状态聚合，不要求固定两个目标；`can_cleanup()` 也按全部目标判断。这两处保留原语义，但需要三目标乱序测试。当前 `scheduler._recover()` 只通过 `dual()` 进入多目标恢复路径，必须改为“目标数 > 1”或统一按 publication 恢复，否则仅选 AcFun 的任务会走旧单平台恢复逻辑。

## 3. 目标结构

```text
tasks(task_id，Bilibili 账号，源视频，唯一共享素材)
  ├─ task_publications[bilibili] → Bilibili 账号 FIFO（最多五条）
  ├─ task_publications[douyin]   → 抖音单账号 FIFO（可选）
  └─ task_publications[acfun]    → AcFun 单账号 FIFO（可选）

共享：下载 1 条 → 校验 1 条 → 准备 5 个 worker（共用翻译服务原锁）
```

满配最多七条上传通道，每条并发 1。父 `task_id` 持有工作目录锁直到所有子目标结束；子目标使用自己的 `publication_id`、状态、快照、取消位、进度、回执和错误。素材只有一份，任何子目标在读取文件时禁止清理。AcFun 的认证失败或限流只暂停 AcFun lane。

`AccountLane` 目前依靠内存 `items` 排序和已有队列序号。新增目标时先在数据库提交目标及可运行顺序，再进入内存队列；启动后沿用当前保守恢复策略，将中断项留给用户手动继续，不自动重新上传。顺序、阶段和等待原因需在持久作业负载中可重建，不能只以线程内列表作为唯一依据。

## 4. 数据迁移和身份

### 4.1 schema v4→v5

1. 持有 profile 独占锁，用 SQLite backup API 生成不覆盖原文件的 `.pre-v5.bak`；事务内进行迁移并运行 `PRAGMA foreign_key_check`。
2. 现有 `task_publications` 的 `CHECK(platform IN ('bilibili','douyin'))` 不能直接扩值，需在事务中建 v5 新表、复制所有行、重建原索引/不可变身份触发器、比对行数及主键，再替换旧表。保留已有 publication_id、抖音结果、BV 和任务引用。
3. 新增 `acfun_accounts`：本地 `account_id`、接入方式版本、AcFun 稳定用户 ID、展示昵称、生命周期、认证状态、绑定版本、创建/更新时刻；约束单活跃账号和身份不可变。扫码 Cookie 不是账号 ID，昵称不能用于去重。稳定 ID 应从经验证的登录态账号响应提取，并在再次扫码时与旧绑定比对。
4. 新增 AcFun 去重唯一索引，键为 `(account_id, source_video_id)` 且 `platform='acfun'`。本地 `account_id` 对应不可变 `(acfun_web_session, AcFun 用户 ID)`；以后更换正式接入方式时需核对是否仍指向同一平台账号，不能因适配器版本变化绕过去重。
5. 旧任务全部保持原目标集合；不自动插入 AcFun publication。更新 `SCHEMA_VERSION`、健康检查的 `schema_version` 和协议能力，旧程序遇到更高 schema 应拒绝启动。

迁移失败时整体回滚并保留备份；跨配置导入含多平台投稿的库仍需遵守现有“整体备份/恢复”限制，不可用旧导入路径重建身份。不能只修改 `PRAGMA user_version`。

### 4.2 目标记录

沿用 `task_publications` 的 `publication_id/task_id/platform/account_id/source_video_id/status/revision/text/remote_id/error/retain_assets/snapshot`。AcFun 平台快照新增标题、简介、分区、标签、原创/转载、原视频 URL、来源说明、封面策略、目标账号身份、适配器版本和用户确认版本；这些字段在投稿前冻结，不能受其他平台编辑影响。Y2A-Auto 使用 `channelId`、`creationType`、`originalLinkUrl`、`tagNames`、`coverUrl`、`videoInfos`；仅作为适配字段清单，不把其值域和长度上限视为官方契约。[投稿字段](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_uploader.py)

AcFun 创建作品的尝试需要本地耐久台账：`publication_id`、attempt/operation ID、素材哈希、请求快照哈希、`intent_recorded_at`、请求可能发出的时间、远端上传 `taskId`、视频素材 `videoId`、最终 `dougaId`、响应/人工核对来源和结果。`createVideo` 与 `createDouga` 分成两个不同的提交边界；前者成功不等于稿件已提交。若网页接口没有原生幂等键或结果查询，台账只能阻止本机自动重复发起，不能证明平台未收到丢失响应的请求。[上传器](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_uploader.py)

### 4.3 原子创建与旧请求兼容

- `tasks.create` 增加 `sync_acfun=false`、`acfun_account_id`、`acfun_binding_revision`。先校验所有已勾选目标的账号、能力和自动模式资格；在**一个**数据库事务中检查 `(video_id, Bilibili account_id)`、抖音去重及 AcFun 去重，创建父任务及全部 publication。任何目标失败整体回滚。
- `operation_id` 的请求指纹包括规范化源 URL、Bilibili 账号、模式、按平台排序的目标集合及其绑定版本。旧请求省略 AcFun 字段时沿用旧哈希规则，保证升级后的重放仍能返回原结果；显式改变目标集合应报冲突。
- 同一个 Bilibili 账号同源任务已存在但目标集合不同，返回已有任务及“目标不可追加”，不能默默改写原目标。不同 Bilibili 账号选择相同 AcFun 账号/同源时，由 AcFun 唯一索引原子拒绝。
- 未选择 AcFun 时 `tasks.create` 不访问 AcFun 会话适配器或远端接口。

## 5. 授权与 AcFun 适配器契约

### 5.1 网页会话、账号身份与能力探测

首期本机适配器参考 Y2A-Auto 的扫码流程：开始扫码、轮询扫码、等待手机确认、从同一 HTTP 会话取得 Cookie。原实现使用 `scan.acfun.cn` 下的 `qr/start`、`scanResult` 和 `acceptResult` 路径；这些是**源码观察到的网页端点，不是官方开放 API 契约**。二维码应有超时、取消与刷新，轮询仅在登录会话有效期内运行。[Y2A-Auto 扫码实现](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_auth.py)

- 从已认证的账号信息响应取得稳定 AcFun 用户 ID，再绑定本地 `account_id`。Y2A-Auto 的 `getMyChannels` 只被用来试登录，不能替代账号身份核验；其对非 JSON HTML 的宽松判定不能沿用。
- Cookie 是可直接代表用户操作的凭据。仅保存在 Windows 系统凭据库或用户 profile 中受保护的凭据容器；不写入任务快照、SQLite、日志、导出包或普通 JSON 文件。加载时校验 Cookie 域和目标主机，网络客户端只允许已审查的 AcFun/上传域名，不把 Cookie 发给上传 CDN。退出登录清除凭据，绑定身份与历史保留。
- “已登录”由可解析、账号 ID 一致的认证响应证明；“可投稿”还需当前版本在真实账号上验证创作中心能力、分区列表和投稿流程。不要以 HTTP 200 或返回 HTML 直接认定可投稿。会话变化、403、登录重定向和响应 schema 变化均暂停 AcFun lane。
- 官方接入资料如可取得，须核对 AcFun 是否允许网页会话自动投稿、自动模式条件、上传限制及作品结果查询能力。缺少许可或真实验证时保持正式发布能力未验收；当前仅允许用户主动启用实验性预览接入，自动模式始终关闭。网页方案的可用性与抖音官方能力分开表示。

### 5.2 接口边界

定义内部 `AcfunPublisher` 契约。字段名表达业务语义，网页端点细节只放适配器里：

```text
capabilities() -> {can_publish, can_auto_publish, experimental, reason, adapter_version}
account() -> {stable_identity, display_name, binding_revision, auth_state}
validate(snapshot, media_info) -> typed validation result
upload_video_media(publication_id, attempt_id, asset) -> remote_upload_task_id + video_id | unknown
upload_cover(publication_id, attempt_id, asset) -> cover_url | unknown
create_douga(publication_id, attempt_id, frozen_snapshot, video_id, cover_url) -> douga_id | unknown
lookup(attempt_id | douga_id) -> submitted | definitely_not_started | unknown
```

HTTP 客户端须固定 HTTPS 与允许的精确主机名、保持证书验证、禁止登录跳转把凭据带去其他域、设置连接/读取超时和最大响应体积。上传用流式分块读取，防止一次把整段大视频载入内存。AcFun 会话不进入抖音 broker；如果以后采用正式服务端授权，再在同一接口后增加独立实现。`acfun_sync_v1` 健康能力只在适配器启用且版本匹配时返回。

参考实现的端点分布在 `scan.acfun.cn`、`member.acfun.cn` 和 `upload.kuaishouzt.com`；各阶段请求方法、字段、签名及错误码必须通过当前网页流程与测试账号重新核对，不能仅复制旧常量。[登录源码](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_auth.py)、[上传源码](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_uploader.py)

### 5.3 投稿边界与未知结果

1. 冻结 AcFun 快照与文件指纹；核对账号 ID、Cookie 会话、媒体与必填字段。参考实现的标题 50、标签最多 6、简介 1000 只作为初始客户端校验候选，须与当前网页和真实账号核实；超限回预览编辑，不静默截断。[参考限制](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_uploader.py)
2. 创建 attempt，持久化上传阶段。按 `getKSCloudToken` 返回的 `partSize` 分块，传输、合并并检查每一步业务响应。分片可能安全重传的条件需实测；不能假设 `complete` 失败可忽略。区分远端上传 `taskId`、本地父 `task_id` 与 publication ID。
3. 调用 `createVideo` 并确认视频素材 `videoId`；调用封面令牌、上传和取封面 URL。两者都是素材阶段，尚未建立 AcFun 稿件。封面临时文件以 task_id/publication_id 隔离，禁止跨任务复用固定文件名。[视频和封面流程](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_uploader.py)
4. 根据冻结的 `title/description/tagNames/channelId/creationType/originalLinkUrl/coverUrl/videoInfos` 构造最终稿件。**先持久写 `createDouga` 意图，再发送请求**。仅业务码成功且返回有效 `dougaId` 才将 publication 置为 `submitted`，展示 AC 号；`createVideo` 的 `videoId` 不能显示成投稿结果。[稿件回执判定](https://github.com/fqscfqj/Y2A-Auto/blob/main/modules/acfun_uploader.py)
5. `createVideo` 或 `createDouga` 请求可能到达平台但响应丢失、进程崩溃或返回异常时，分别记录“素材结果不确定”和 `submission_unknown`，保留素材与账号关联。先尝试经真实验证的作品查询；若无可靠查询或幂等键，让用户到 AcFun 稿件管理核对，并在原 publication 人工登记结果。不得自动重发 `createDouga`。
6. 限流、会话过期、字段校验失败和瞬时网络故障分类处理：限流/撤权暂停 AcFun lane；可修复字段错误回预览；网络故障在创建边界前可按已验证的阶段恢复，创建边界后只核对。日志输出错误类别和脱敏业务码，不记录原始 Cookie、上传 token 或完整平台响应。

网页接口如没有可证明的创建幂等或结果查询能力，断线恢复只能保持“待核对”，不能承诺自动重试无重复稿件。这一限制同样适用于参考仓库声称的自动上传能力。

## 6. 调度、状态和资源生命周期

- 将 `scheduler.fanout()` 的二分支路由替换为平台队列映射。`AccountLane.work()` 调用注册的 publisher；现有 Bilibili `UploadCoordinator` 原样保留其 UID 文件锁和尝试标记。AcFun lane 单账号、并发 1、独立冷却及唤醒。
- `scheduler.snapshot()` 返回 `acfun` 队列的当前 task/publication、等待数、原因；前端按 publication_id 保存平台上传进度。下载/校验/准备进度仍归父 task_id。
- `publications.prepare()` 对每个未结束目标建立预览状态；AcFun 专属校验失败只改 AcFun 为 `blocked_validation`。预览确认冻结全部已选目标；自动模式对获准且校验通过的目标分别入队。
- 父摘要继续由 publication 集合投影：全提交、部分提交、待核对、部分放弃等状态；列表和详情必须保留每个子状态，不能只看父 `status` 触发重投。
- `scheduler._recover()` 统一按 publication 集合处理，`creating`/可能已发起的提交标为未知；已确认排队但中断的目标人工继续；已提交目标保持回执。`publications.dual()` 改为更明确的 `multi_target()`，或统一让所有任务走同一恢复路径。
- `can_cleanup()` 仍要求每个已选目标均 `submitted` 或 `abandoned` 且没有保留标志；最后一个子目标释放父目录锁后再清理。AcFun 上传中和待核对时不得清理。

## 7. 桌面 RPC、界面与 CLI

- 新增 `acfun.auth.status/start/poll/cancel/clear`、`acfun.accounts.archive/resume_uploads`；`start` 返回一次性二维码图像/过期时间，`poll` 返回扫码、手机确认、完成或失败状态。窗口关闭取消轮询。适配器不可用时返回明确原因，不创建假账号或假会话。
- `tasks.create` 接收第 4.3 节新字段，`tasks.get/list` 返回 `platform='acfun'` 的 publication；已有 `publications.update_metadata/retry/cancel/abandon/resolve` 扩展到 AcFun，保留按目标权限和状态检查。
- `desktop/src/types.ts` 增加 `acfun` 联合类型，替换界面中“抖音否则 Bilibili”的二元标签。新建弹窗分别查询平台能力，默认不勾选；身份变化更新 operation_id。目标不可用时保留用户填写的 URL、账号、模式和其他已选平台。
- 账号页增加 AcFun 卡片；详情页增加 AcFun 元数据编辑和作品回执；队列卡片新增 AcFun lane。`desktop/src/preview.ts` 和 Playwright 仅模拟服务端资格，不把 mock 成功当作真实发布。
- CLI 可新增 `run --sync-acfun`，默认关闭；与 `--sync-douyin` 可并用。现有 `--auto` 需检查每个已选目标的自动资格。健康检查增加 `acfun_sync_v1` 能力，前端/worker 版本不匹配时禁用新开关，避免旧 worker 忽略 AcFun 目标。

## 8. 实施顺序、测试与验收

1. **参考链路复核**：以 Y2A-Auto 源码为调查线索，核对当前网页流程、AcFun 条款、账号 ID、扫码/会话、分片、视频素材、封面和 `dougaId`；在本项目测试账号获得真实可核对的回执。没有结果时保持默认关闭、标记实验性，并禁用自动模式。
2. **多目标基础**：v5 迁移、平台注册表、通用路由、AcFun 账号与去重、原子创建；确保旧 Bilibili/抖音路径回归。
3. **模拟适配器与 UI**：使用可控假服务验证三平台并发、AcFun FIFO、单边失败、账号变更、限流、进程重启、未知结果、素材保留和前端三目标显示。
4. **本机网页会话适配器**：独立实现扫码和会话保管、媒体/封面上传、作品创建与本地意图台账；使用当前实测字段，严格核验各阶段响应，不直接复制 Y2A-Auto GPL-3.0 源码。若决定复用代码，先评估许可证兼容性。[Y2A-Auto 许可证](https://github.com/fqscfqj/Y2A-Auto/blob/main/LICENSE)
5. **真实环境验收**：在用户授权和有权发布的素材上验证扫码、普通/大文件、平台元数据、`dougaId`、实际审核可见性、断线恢复、会话过期与发布频率。单列未通过项和网页接口变更处理后，再按实验性功能打开 AcFun 开关。

必测断言：三目标仅下载/翻译一次；Bilibili 最多五路、抖音一路、AcFun 一路；AcFun 阻塞时其他平台完成；AcFun 同源同账号并发去重；HTTP 200 的 HTML/错误 JSON 不算登录或投稿成功；分片、合并、`createVideo`、封面任一步失败不进入 `createDouga`；`createDouga` 前后断线不会重复调用创建；三目标结果乱序不提前删素材；v4 库升级后抖音账号和投稿原样保留；未选择 AcFun 的任务完全不触发 AcFun 远程请求。沿用现有 Python 回归、TypeScript 构建和 Playwright，真实平台验收单独记录。

## 9. 未决问题

- AcFun 当前条款是否允许使用网页会话自动投稿？是否有可替代的正式第三方接入能力？
- 网页接口是否提供可靠的稿件查询、去重或幂等键，以证明响应丢失后的结果？
- 当前媒体、封面、标题、简介、分区、标签、转载与来源字段及限制是什么？Y2A-Auto 的 50/6/1000 数值是否仍适用？
- 扫码后的稳定账号 ID 应从哪个已认证响应读取？会话有效期和撤销如何可靠检测？
- 本机直传是否满足所有网络与文件大小场景？若以后需要中转，如何保持 Cookie 与稿件台账隔离？

以上问题是上线门槛。文中的网页端点和字段是 Y2A-Auto 公开源码里的观察值，不是 AcFun 官方稳定接口声明；本项目尚未执行参考代码或真实投稿。资料核对日期：2026-09-26。
