# 已有库升级

备份后在测试副本验证，再停止采集并执行 001_message_id_index.sql。
这次移除 msg_id 唯一约束，保留 seq 主键，避免不同事件共享 msg_id 时静默吞掉记录。
迁移脚本仅针对旧版 schema 中存在 uk_msg_id 的数据库，执行一次；新库直接用 schema.sql。
历史 decrypt_failed 占位记录和丢失附件不会自动修复，需根据记录时间、官方可获取窗口及备份人工恢复。不要直接回退生产游标。
