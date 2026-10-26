# Contributing

请用合成消息构造回归测试，不提交腾讯专有 SDK 或真实企业数据。
执行 `python -m unittest discover -s tests -p 'test_*.py' -v`。
涉及 SDK ABI 的改动须说明 SDK 版本、操作系统、CPU 架构及真实联调结果。
数据库结构变更须同时提供已有库迁移说明。提交贡献按项目 MIT 许可证授权。
