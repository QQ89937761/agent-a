# 项目维护约定

- 本仓库按 PolyForm Noncommercial License 1.0.0 发布；禁止商业使用，商业用途必须先取得单独书面授权。
- 不提交真实密钥、个人配置、数据文件、服务器地址或部署记录；仅保留明确标注的示例配置。
- 普通源码提交运行 GitHub Actions 检查；仅推送符合版本号的 `v*` 标签时构建并发布便携版。
- Portable 发布须通过完整测试、哈希校验和更新冒烟后才能生成 GitHub Release。
- 不增加私有下载站、下载口令或隐藏分发凭据。自动更新通道由 backend/core/update_manager.py 中的 UPDATE_REPOSITORY 配置，须使用经审查的公开 GitHub Releases 地址。
