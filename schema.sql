-- 企业微信「会话内容存档」落库结构 —— MySQL 8.0 版
-- 要求：MySQL >= 8.0（依赖 SKIP LOCKED / ngram 全文索引 / utf8mb4_0900_ai_ci）
-- 字符集必须 utf8mb4；不要用 utf8(mb3)，否则 emoji 直接 1366 报错。
--
-- 时区约定：所有 DATETIME 都是 Asia/Shanghai 的「本地墙钟时间」，不带时区。
--           msg_time 由企微返回的毫秒时间戳换算而来，见 src/normalize.py。

CREATE DATABASE IF NOT EXISTS `wecom_chatdata`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `wecom_chatdata`;

-- ---------------------------------------------------------------
-- 1. 拉取游标：单行表，id 恒为 1
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `pull_cursor` (
  `id`         TINYINT UNSIGNED NOT NULL,
  `last_seq`   BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已成功落库的最大 seq，严格递增',
  `last_err`   VARCHAR(512) NULL,
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='拉取游标，GetChatData 的 seq 从这里往后取';

INSERT INTO `pull_cursor` (`id`, `last_seq`) VALUES (1, 0)
  ON DUPLICATE KEY UPDATE `id` = `id`;

-- ---------------------------------------------------------------
-- 2. 会话（群）
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `chat_room` (
  `room_id`       VARCHAR(160) NOT NULL,
  `room_name`     VARCHAR(255) NULL,
  `first_seen_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `last_msg_time` DATETIME(3) NULL,
  `msg_count`     BIGINT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`room_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------
-- 3. 消息主表
--    seq   = 企业微信服务端全局递增序号，天然主键、天然幂等
--    msg_id = 企微消息唯一 id，去重第二道保险
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `chat_message` (
  `seq`         BIGINT UNSIGNED NOT NULL COMMENT 'GetChatData 返回的 seq',
  `msg_id`      VARCHAR(96)  NOT NULL,
  `action`      VARCHAR(24)  NOT NULL DEFAULT 'send' COMMENT 'send/recall/switch',
  `from_user`   VARCHAR(128) NOT NULL,
  `room_id`     VARCHAR(160) NULL COMMENT '空=单聊',
  `msg_type`    VARCHAR(32)  NOT NULL COMMENT 'text/image/voice/video/file/link/...',
  `msg_time`    DATETIME(3)  NOT NULL,
  `content`     LONGTEXT     NULL COMMENT '归一化后的可读文本，供全文检索',
  `raw`         LONGTEXT     NOT NULL COMMENT '解密后完整 JSON 原文，永不丢字段',
  `media_count` SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  `created_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`seq`),
  KEY `idx_msg_id` (`msg_id`),
  KEY `idx_room_time` (`room_id`, `msg_time`),
  KEY `idx_from_time` (`from_user`, `msg_time`),
  KEY `idx_type_time` (`msg_type`, `msg_time`),
  FULLTEXT KEY `ft_content` (`content`) WITH PARSER ngram
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------
-- 4. 收件人（单聊/群成员的 tolist）
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `chat_message_to` (
  `seq`     BIGINT UNSIGNED NOT NULL,
  `user_id` VARCHAR(128) NOT NULL,
  PRIMARY KEY (`seq`, `user_id`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------
-- 5. 媒体文件队列
--    sdkfileid 获取期限以当前官方规则为准 —— deadline_at 过期后基本无解，
--    所以这里既要「尽早在有效期内抢下来」，也要能识别「已经救不回来」。
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `media_object` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `seq`           BIGINT UNSIGNED NOT NULL,
  `msg_id`        VARCHAR(96) NOT NULL,
  `msg_time`      DATETIME(3) NOT NULL COMMENT '所属消息时间，用于存储路径分层',
  `kind`          VARCHAR(16) NOT NULL COMMENT 'image/voice/video/file/emotion',
  `file_id`       VARCHAR(512) NOT NULL COMMENT 'sdkfileid 原文（可能是长串）',
  `file_id_hash`  CHAR(64) NOT NULL COMMENT 'sha256(file_id)，唯一键（512 长度的列不能直接做唯一索引）',
  `filename`      VARCHAR(255) NULL,
  `file_md5`      VARCHAR(40) NULL,
  `file_size`     BIGINT UNSIGNED NULL,
  `status`        ENUM('pending','downloading','done','failed','expired')
                  NOT NULL DEFAULT 'pending',
  `storage_key`   VARCHAR(512) NULL,
  `attempts`      TINYINT UNSIGNED NOT NULL DEFAULT 0,
  `next_retry_at` DATETIME(3) NULL,
  `last_error`    VARCHAR(512) NULL,
  `deadline_at`   DATETIME(3) NOT NULL COMMENT 'msg_time + 3 天',
  `created_at`    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_file_id_hash` (`file_id_hash`),
  KEY `idx_claim` (`status`, `next_retry_at`),
  KEY `idx_seq` (`seq`),
  KEY `idx_deadline` (`status`, `deadline_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
