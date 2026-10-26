# 单企业真实采集联调

Fake 和 real 使用不同 Compose 项目名/数据卷，不能复用 fake 游标。以下统一使用 `-p wac-real`。

1. 从官方获取与容器架构一致的 SDK v3（OpenSSL 3），将 C_sdk 中的 `libWeWorkFinanceSdk_C.so` 放在 `lib/`。不要使用 Java 库，不要提交 SDK。
2. 本地生成 RSA 密钥，将公钥配置到企微，私钥存放 `secrets/key-v1.pem`；文件所有者/组应允许容器 UID 10001 读取，不能简单设置成其他所有者的 600 后期待容器可读。
3. `cp config.real.example.yaml config.real.yaml`，修改企业 ID 和 private_key_paths 的公钥版本。`.env` 设置 MYSQL_PASSWORD、MYSQL_ROOT_PASSWORD 和 WECOM_CHATDATA_SECRET，不要上传。
4. 企业管理员确认开通范围、客户授权、可信出口 IP。公网回调页面不是本采集工具的必要前置。

先做不访问业务 API 的检查：

```bash
docker compose -p wac-real -f compose.yaml -f compose.real.yaml build collector
docker compose -p wac-real -f compose.yaml -f compose.real.yaml run --rm --no-deps collector check-sdk
```

check-sdk 只验证 SDK 加载/初始化和本地私钥解析，不验证 Secret、可信 IP、权限或网络是否有效。

初始化隔离数据库，执行首次消息和附件采集（不要同时启动常驻 collector）：

```bash
docker compose -p wac-real -f compose.yaml -f compose.real.yaml up -d db
docker compose -p wac-real -f compose.yaml -f compose.real.yaml run --rm init
docker compose -p wac-real -f compose.yaml -f compose.real.yaml run --rm --no-deps collector pull --once
docker compose -p wac-real -f compose.yaml -f compose.real.yaml run --rm --no-deps collector media
docker compose -p wac-real -f compose.yaml -f compose.real.yaml run --rm --no-deps collector stats
```

发送唯一文字、图片、语音、文件、群聊和复合消息；核对 raw、收发人、时间与附件实际内容。逐次对照原件大小和 MD5，不仅看状态 done。

通过后常驻运行：

```bash
docker compose -p wac-real -f compose.yaml -f compose.real.yaml up -d collector
docker compose -p wac-real -f compose.yaml -f compose.real.yaml logs --tail=100 collector
```

每轮最多处理 media.max_items_per_round 个附件，然后返回消息拉取；限制的是数量，单个慢附件仍可能延迟下一轮。每个任务结束释放媒体线程的数据库连接，主线程保持采集锁。

仍需监控解密阻塞、failed 附件、磁盘容量和数据库备份。一次 SDK 加载通过不代表真实采集已经通过。

已在 ARM64 Debian Bookworm Docker 镜像中加载官方 ARM v3 20250205 库，并验证 SDK/Slice/MediaData 分配释放；未使用真实企业凭证调用业务接口。SDK 源自腾讯官方，未随源码或镜像分发。
