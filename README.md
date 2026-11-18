# vpn-monitor · 本机 VPN 流量监控

基于 Clash Verge / Mihomo 本地 API 的代理流量明细统计工具。纯 Python 标准库实现，无需第三方依赖。

## 功能

- 自动选择本机 API：Windows 命名管道、macOS/Linux Unix socket、HTTP 外部控制
- 按应用、应用路径、域名/IP 统计代理上传与下载字节
- SQLite 保存分钟明细与连接记录，每日导出 `data/YYYY-MM-DD.csv`
- 本地网页看板：时间范围、路由、应用、域名筛选，明细 CSV 导出
- 可一键集成到 Clash Verge：启动脚本、网页界面入口、开机自启

## 环境要求

- Clash Verge Rev（Mihomo 内核），且至少启动过一次
- Python 3.8+，只用标准库，无需 `pip install`
- Windows 10/11 或 macOS；Linux 可手动运行

## 快速开始（集成到 Clash Verge）

Windows：

1. 双击 `安装.cmd`
2. 重启一次 Clash Verge
3. 打开 Clash Verge「网页界面」中的 `127.0.0.1:18769`，或直接访问 http://127.0.0.1:18769/

macOS：

1. 终端执行 `bash setup.sh`
2. 重启一次 Clash Verge
3. 从 Clash Verge「网页界面」打开统计页，或访问 http://127.0.0.1:18769/

安装脚本会自动：

1. 把 Clash Verge 的启动脚本指向 `start.ps1` / `start.sh`
2. 在网页界面列表中加入 `http://127.0.0.1:18769/`，保留原有面板
3. 安装开机自启（Windows 登录启动项 / macOS LaunchAgent）
4. 启动监控

脚本幂等，可重复执行；修改 `verge.yaml` 前会自动备份为 `verge.yaml.bak-*`。

可选参数：

- `python setup.py --dry-run`：只查看将要修改的内容
- `python setup.py --no-autostart`：不安装开机自启

## 单独使用（不修改 Clash 配置）

Windows：

```text
启动监控.cmd    # 启动
停止监控.cmd    # 停止
```

macOS：

```bash
bash start.sh   # 启动
sh stop.sh      # 停止
```

前台运行：`python vpn_monitor.py`（macOS 为 `python3 vpn_monitor.py`），`Ctrl+C` 停止。

单独使用仍需 Clash Verge 正在运行，只是不会写入 Clash Verge 配置，也没有开机自启。两种方式共用同一份数据，可随时切换。

## 自检

```bash
python vpn_monitor.py --probe        # 输出 API 类型、连接数、代理连接数，不写记录
python test_vpn_monitor.py           # 18 项单元测试，使用临时数据库
```

`--probe` 输出示例：

```json
{"api": "pipe", "connections": 53, "proxy": 25, "with_process": 49}
```

## 数据与隐私

- 数据目录：`data/`，包含每日 CSV、`traffic.sqlite3`、运行日志
- 所有记录只保存在本机，不上传网络；不保存 API 密钥、订阅地址或请求内容
- `data/` 含本机域名、进程路径和历史流量，分发或提交代码时请排除
- 页面与 API 只监听 `127.0.0.1:18769`

## 卸载

1. 停止监控：Windows `停止监控.cmd`，macOS `sh stop.sh`
2. 移除自启：Windows `取消开机启动.cmd`；macOS 执行 `launchctl unload ~/Library/LaunchAgents/io.github.clash-verge-rev.vpn-monitor.plist` 并删除该 plist
3. 在 Clash Verge 中删除启动脚本和网页界面条目
4. 需要清除历史记录时，删除整个目录

## 常见问题

1. 页面显示「等待 Mihomo」：先启动 Clash Verge，工具会自动重试。
2. 页面打不开：手动执行启动脚本，确认 `127.0.0.1:18769` 未被占用。
3. 数据库报 `disk I/O error`：停止监控，备份并移走 `traffic.sqlite3-wal`、`traffic.sqlite3-shm` 后重启。
4. macOS 找不到 `python3`：安装 python.org 的 Python 3，或执行 `xcode-select --install`。

完整说明见 [使用说明.md](使用说明.md)。
