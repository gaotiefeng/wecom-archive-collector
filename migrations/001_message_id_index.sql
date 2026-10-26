ALTER TABLE chat_message DROP INDEX uk_msg_id, ADD INDEX idx_msg_id (msg_id);
