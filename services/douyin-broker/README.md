# 抖音官方授权与投稿服务

此服务为单用户/单租户、单实例部署，桌面应用保留调度权。不要把同一配对密钥提供给不同用户，也不要横向启动多个服务实例。`python -m yt2bili.douyin_broker` 持有数据目录进程锁，拒绝重复实例。

## 必要前提

- 在抖音开放平台创建适用的网站应用，取得 `video.create.bind` 能力及用户授权。用户登录不等于应用获得发布权限。
- 准备已备案/配置符合平台要求的 HTTPS 域名、有效 TLS 证书、固定授权回调 `https://你的域名/oauth/callback`。
- 自动投稿默认关闭。仅当你的应用和使用场景获得官方允许时，部署人员才应设置 `DOUYIN_APPROVED_AUTO_PUBLISH=1`；这个开关不能代替平台权限申请。
- 本次开发未部署公网服务、未申请权限、未进行真实抖音发布。

## 安装与配置

在服务主机的独立虚拟环境中安装项目与服务额外依赖：

```text
python -m pip install '.[douyin-broker]'
```

通过部署系统的秘密配置注入以下环境变量，不要写入 Git、桌面设置或日志：

| 名称 | 用途 |
| --- | --- |
| `DOUYIN_CLIENT_KEY` | 官方应用标识 |
| `DOUYIN_CLIENT_SECRET` | 官方应用密钥，仅服务端持有 |
| `DOUYIN_CALLBACK_URL` | 注册的完整 HTTPS `/oauth/callback` 地址 |
| `DOUYIN_PAIRING_KEY` | 至少 32 字符的高熵随机密钥，用于一位桌面用户配对 |
| `DOUYIN_ENCRYPTION_KEY` | Fernet 密钥，令牌静态加密；与数据库分开备份 |
| `DOUYIN_APPROVED_AUTO_PUBLISH` | 可选，默认关闭，官方批准后设为 `1` |

可在服务主机用 `secrets.token_urlsafe(48)` 生成配对密钥，用 `cryptography.fernet.Fernet.generate_key()` 生成加密密钥。生成值仅保存到秘密管理器，不要粘贴到任务日志。

启动命令（`--data-dir` 使用专用目录）：

```text
python -m yt2bili.douyin_broker --data-dir /srv/yt2bili-douyin --port 8787
```

进程仅监听 `127.0.0.1:8787`。必须在同一主机部署 HTTPS 反向代理并提供认证、请求速率、连接数和存储容量防护。外部只开放 HTTPS，不开放内部 HTTP 端口。代理不得记录 Authorization、OAuth code/state 或请求正文；OAuth 回调访问日志同样须禁用/脱敏。对 `/v1/*` 不启用跨域 CORS。

反向代理需允许最大 4 GiB 视频请求及长连接；配置足够的上传/响应超时，禁用代理重试 POST。服务端会流式写入临时文件，然后分片上传到抖音（内存不随整个大视频增长），因此需要额外磁盘空间和中转带宽，不是纯本地直传。只有到平台创建作品阶段才可能产生发布副作用。

数据库保存加密后的平台令牌和持久投稿回执，文件夹只允许服务运行用户访问；Windows 部署须配置等效 ACL。临时视频正常请求结束即删除；进程崩溃残留的 `*.upload` 文件应在停止服务并核实无在途上传后由运维清理。不得删除 `broker.sqlite` 来“重试”，否则会失去防重复的历史记录。

## 桌面使用

1. 打开“账号与连接”→“抖音同步投稿”，填写 HTTPS 服务源地址和配对密钥。
2. 点击“在浏览器授权抖音”，在官方页面完成登录授权，再返回点击“检查授权状态”。
3. 新建任务时勾选“同步上传抖音”；每次新建默认不勾选，未登录时禁用。
4. 预览模式下可单独编辑抖音文案，默认取同一份译文的标题。主确认按钮一起提交准备好的目标；自动模式将两个目标分别自动入队。
5. 各平台结果独立展示，单平台失败通过“继续此目标并预览”恢复。限流需在账号页明确恢复抖音队列。结果不明必须核对创作中心，不会自动重发创建作品。

退出登录保留身份和历史；更换账号前必须完成、核对或明确放弃旧账号未结束的抖音目标，然后归档。Bilibili 未结束目标不阻止抖音账号归档。

## 恢复、安全边界与当前范围

- 媒体上传和创建作品严格分开。创建前落盘 intent，成功回执落盘后再响应桌面；网络结果不明或服务重启遇到 `creating`，转为 `submission_unknown`，相同 ID 不再次创建。
- 回调 state 随机、10 分钟有效、一次性消费；取消或新建授权使旧回调失效。
- 同源视频与同抖音身份有唯一约束；同一服务内跨桌面 profile 也不会另建重复记录。
- 跨**不同服务数据库**的多实例不保证去重，当前不支持该部署方式。
- 令牌到期按官方刷新接口续期；刷新失败需重新授权。新建任务验证和真实上传前验证都有账号身份检查；服务发现官方明确的授权错误时暂停该抖音目标。
- 桌面 v3 数据升级到 v4 前会产生独立 `.pre-v4.bak`；旧历史不会自动添加抖音目标。现有“导入另一目录”可导入仅 B 站的 v4 数据；含抖音记录时明确拒绝旧式重建身份导入，请整体备份/恢复 profile（系统密钥库需重新配对）。
- 手工核对“已提交”保留本地素材；有失败、取消、待预览、待核对、等待或在途目标时不自动清理。
- 不支持小红书、不支持多抖音账号、不抓取私人 Cookie、不绕过验证码或平台权限。

## 接口依据与真实验收

对接依据：[授权码](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/account-permission/douyin-get-permission-code)、[创建视频](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-create)、[分片初始化](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-part-upload-init)、[分片完成](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-part-upload-complete)、[封面上传](https://partner.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-image-text/image-upload)。

官方不同上传文档包含历史限制描述，本实现采用视频上限 4 GiB、时长 15 分钟、超过 50 MiB 分片，均衡分片约 20 MiB 避免尾片小于 5 MiB；封面在本产品服务侧进一步限制 20 MiB。最终需用**已获权限的测试应用和用户授权内容**验收小视频、大文件分片、OAuth 回调、过期刷新、限流、错误码、平台审核结果。单元测试和本地浏览器测试不代表真实平台验收。
