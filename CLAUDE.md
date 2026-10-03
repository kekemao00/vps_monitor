# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

搬瓦工 (BandwagonHost) VPS 流量监控系统。通过搬瓦工 API 定时采集多台 VPS 的流量数据，生成 Markdown 格式报告并推送至钉钉群机器人。由 crontab 驱动定时执行。

## 运行方式

```bash
# 手动执行（在脚本目录下）
python3 vps_monitor.py

# 指定配置文件
python3 vps_monitor.py --config /path/to/config.json

# 只打印消息、不发送不写历史
python3 vps_monitor.py --dry-run

# 单元测试
python3 -m unittest discover -s tests

# 生产部署路径: /opt/vps_monitor/
# crontab 示例 (每天 9:00 和 21:00):
# 0 9,21 * * * /usr/bin/python3 /opt/vps_monitor/vps_monitor.py >> /opt/vps_monitor/cron.log 2>&1
```

依赖: `requests>=2.28.0` (见 requirements.txt)

## 架构

单文件 Python 脚本 (`vps_monitor.py`)，四个核心类协作:

- **CronParser** — 解析当前用户 crontab，计算下次执行时间，用于报告底部展示
- **DingTalkRobot** — 钉钉自定义机器人封装，支持 HMAC-SHA256 加签认证，发送 Markdown 消息
- **BandwagonAPI** — 搬瓦工 API 客户端，调用 `getServiceInfo` + `getLiveServiceInfo` 端点 (`api.64clouds.com/v1`)
- **VPSMonitor** — 核心编排类，并发查询 VPS → 计算流量/预测超标 → 组装分层报告 → 调用钉钉发送

数据流: `config.json` → `BandwagonAPI` (并发) → `VPSMonitor.calculate_bandwidth()` → `DingTalkRobot.send_markdown()` + 可选独立紧急告警消息

辅助函数: `load_last_history()` 读取上次执行记录用于趋势对比，`save_history()` 追加写入 `history.jsonl`

## 配置文件 (config.json)

- `dingtalk.webhook` / `dingtalk.secret` — 钉钉机器人凭据
- `vps_list[]` — 每台 VPS 的 `veid`、`api_key`、`name`（不能为空）
- `monitor.critical_threshold` — 🔴 严重预警阈值 (默认 90)
- `monitor.alert_threshold` — 🟠 预警阈值 (必填，如 80)
- `monitor.warning_threshold` — 🟡 警告阈值 (默认 60)
- 阈值顺序约束: `warning < alert < critical`

敏感字段支持环境变量覆盖: `DINGTALK_WEBHOOK`、`DINGTALK_SECRET`、`VPS_{veid}_API_KEY`

## 关键逻辑

- 流量计算: `plan_monthly_data` 不乘倍率，`data_counter` 乘以 `monthly_data_multiplier` 得到计费量
- 预警分四级: 🟢 <warning / 🟡 warning-alert / 🟠 alert-critical / 🔴 ≥critical，边界完全由配置驱动
- 流量超标预测: 基于日均使用量线性外推至重置日
- 消耗速率分级: `burn_rate` = 实际日均 / 理论均匀日均，>1.5 标记⚡偏快，<0.5 标记🐢很低
- 钉钉消息在有 ≥alert_threshold 的 VPS 时触发 @所有人；存在 🔴 时改由独立告警 @所有人，主报告不再 @，避免重复提醒
- 🔴 级别额外发送独立紧急告警消息（Markdown 格式，@所有人）
- 报告分层展示: ≥warning 的 VPS 展开详情，正常 VPS 折叠为一行摘要
- 历史对比: 从 `history.jsonl` 读取上次数据，展示趋势箭头 (↑↗→↘)

## 注意事项

- 路径以脚本所在目录为基准（`SCRIPT_DIR`），支持 `--config` 参数覆盖
- 日志使用 RotatingFileHandler，5MB 轮转，保留 3 份
- API 调用和钉钉发送均有指数退避重试（3次）
- 全部 VPS 查询失败时会发送钉钉失败通知（列出每台失败原因），进程退出码为 1
- 报告标题即通知预览，直接点名最严重的 VPS 与使用率
- 异常日志中 URL 携带的 api_key / access_token / sign 会被 `redact()` 脱敏
- 周期起始日按自然月反推（`data_next_reset` 往前一个月），日均用带小数的已过天数计算
