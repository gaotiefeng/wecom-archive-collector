# WeCom Archive Collector

单企业企业微信会话内容存档采集工具：拉取、解密、MySQL 入库、附件下载。
不包含管理后台、敏感词风控、语音转写。当前为待真实 SDK 联调的预发布版本，不能声称已通过生产验证。

## 环境与边界

- Python 3.11+、MySQL 8.0+（utf8mb4、ngram、SKIP LOCKED）。
- macOS 可运行 fake 模式；真实采集部署在官方 C SDK 支持的 Linux/CPU 架构上。
- 官方接口开通、员工范围、客户授权、可信出口 IP 均由企业管理员配置。
- 腾讯专有 SDK 不随项目分发，不能将 Java JNI 动态库当作 C SDK 使用。
- 单企业、单消息拉取进程；同库使用 MySQL 命名锁避免重复启动。
- 数据库连接丢失时停止本轮处理；常驻循环等待数据库恢复后仍需重启进程以重新获取锁。

## 本地快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

编辑 `config.yaml` 的 MySQL 连接，使用专用测试数据库。模板默认 `mode: fake`。

```bash
python -m src.cli init-db
python -m src.cli pull --once
python -m src.cli media
python -m src.cli stats
python -m src.cli search 报价单
```

fake 附件是合成字节，不是真实可打开的图片/PDF。fake 仅用于空的测试数据库，不能与真实模式共库；切换企业也必须换数据库及附件目录。

## 真实采集

1. 从企业微信官方渠道获取 C SDK，按下载包说明安装其运行依赖。
2. 配置 `wecom.sdk_lib_path` 为 `libWeWorkFinanceSdk_C.so` 的路径。
3. 私钥在本地生成，将公钥配置到企业微信，私钥放 `secrets/`，权限建议 `600`。
4. 设置 `mode: real`、企业 ID、会话存档 Secret、私钥路径和可信出口 IP。
5. 密钥轮换时配置 `private_key_paths`，每个 `publickey_ver` 对应一个 PEM 文件。配置版本映射后不回退到默认私钥。
6. 先 `pull --once`，再执行 `media`，核对真实文字、图片、文件、语音和复合消息。
7. 确认无误后 `python -m src.cli run`，交给 systemd 等进程管理器运行。

C ABI 按 `DecryptData(已RSA解密的key, encrypt_msg, Slice*)` 封装；上线必须用所下载 SDK 的头文件核对结构体和函数声明，并进行真实联调。项目不是腾讯官方 SDK。

## 数据完整性

- `chat_message` 保存公共字段及解密 JSON；`raw` 是 JSON 重新序列化的结果，不保证字节级原文一致。
- 处理任一消息失败，整批不保存、不推进游标；修复密钥/格式问题后重拉。当前没有密文隔离队列，因此持续失败会阻塞采集，必须告警并尽快处置，不能超过官方可获取窗口。
- 消息、收件人、附件任务与采集游标在同一事务提交。
- 附件独立重试，达到上限进入 `failed`，不会把任意 SDK 错误当作过期。
- 默认 `ttl_hours: 0`，不根据未经确认的固定期限跳过下载。若人为设置期限，将按消息时间计算并放弃超期任务，请谨慎使用。
- 本地附件临时写入、校验大小/MD5 后原子改名；S3 临时落盘、校验后上传。
- 支持常见媒体及嵌套/JSON content 中的 `sdkfileid`，仍需用当前官方消息样例核对覆盖范围。
- 媒体任务 `failed`、长期未推进的游标需要监控；当前没有外部告警集成。

## 存储

默认本地 `var/files/`，数据库记录相对 key。附件名包含任务 ID，避免同消息同名文件覆盖。
本地目录必须持久化并备份，不能直接作为无鉴权的公网静态目录。
使用 S3 兼容存储需 `pip install boto3`，配置私有桶；推荐使用环境凭证或实例角色，不把密钥写入公共仓库。

五张表见 `schema.sql`。同一媒体标识当前按文件去重，附件表保留首次关联消息，其他消息仍可通过 raw 内媒体标识关联。
现有库升级见 `migrations/README.md`。初始化命令仅适用于新库，不替代数据库迁移。

## 测试

不访问企微、不访问数据库的测试：

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

`tests/smoke.py` 是 MySQL 集成测试，会删除并重建配置库名加 `_test` 的数据库。只允许在明确隔离的本地测试实例执行，不能给它生产连接。此测试覆盖 fake 流程，不验证真实 SDK ABI。

## 发布前检查

- 真实 Linux SDK 联调通过（含分片附件、密钥轮换、断网重试、重启续拉）。
- 对照最新官方规则确认消息范围、拉取窗口及媒体获取限制。
- 备份恢复演练、存储容量监控、failed 任务监控。
- 发布前检查暂存区；不要上传 config.yaml、secrets、var、SDK 或聊天样本。

当前账号使用最小数据库权限；`init-db` 需要额外建库/建表权限，运行时可以移除。

## License

MIT，见 LICENSE。腾讯 SDK 和其他依赖各自遵循其许可证。

## 容器运行

Dockerfile 不包含数据库、配置、媒体或腾讯 SDK。构建后需要挂载只读配置和 SDK 目录、私钥目录，以及可写的媒体目录。容器以 UID 10001 运行，挂载目录须给该 UID 适当权限。根据官方 SDK 的依赖清单补充系统库；镜像尚未做真实 SDK 构建与运行验证。容器中的数据库地址不能填宿主机含义的 `127.0.0.1`。


## 项目名称与迁移

仓库名称建议：`wecom-archive-collector`。这是独立的社区采集工具，与腾讯及其他同名存档产品无隶属关系。

移动项目后，请在新目录重新创建 Python 虚拟环境；旧虚拟环境的激活脚本和命令入口可能仍引用旧路径。本地配置与附件已保留，但不应提交。MySQL 库名可继续使用 `wecom_chatdata`，无需因仓库改名而修改。
