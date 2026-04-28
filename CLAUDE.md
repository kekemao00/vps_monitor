# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

搬瓦工 (BandwagonHost) VPS 流量监控系统。通过搬瓦工 API 定时采集多台 VPS 的流量数据，生成 Markdown 格式报告并推送至钉钉群机器人。由 crontab 驱动定时执行。

## 运行方式

```bash
# 手动执行
python3 /opt/vps_monitor/vps_monitor.py

# 生产部署路径: /opt/vps_monitor/
# crontab 示例 (每天 9:00 和 21:00):
# 0 9,21 * * * /usr/bin/python3 /opt/vps_monitor/vps_monitor.py >> /opt/vps_monitor/cron.log 2>&1
```

依赖: `requests` (唯一第三方依赖，无 requirements.txt)

## 架构

单文件 Python 脚本 (`vps_monitor.py`)，四个核心类协作:

- **CronParser** — 解析当前用户 crontab，计算下次执行时间，用于报告底部展示
- **DingTalkRobot** — 钉钉自定义机器人封装，支持 HMAC-SHA256 加签认证，发送 Markdown 消息
- **BandwagonAPI** — 搬瓦工 API 客户端，调用 `getServiceInfo` 端点 (`api.64clouds.com/v1`)
- **VPSMonitor** — 核心编排类，批量查询 VPS → 计算流量/预测超标 → 组装 Markdown 报告 → 调用钉钉发送

数据流: `config.json` → `BandwagonAPI.getServiceInfo()` → `VPSMonitor.calculate_bandwidth()` → `DingTalkRobot.send_markdown()`

## 配置文件 (config.json)

- `dingtalk.webhook` / `dingtalk.secret` — 钉钉机器人凭据
- `vps_list[]` — 每台 VPS 的 `veid`、`api_key`、`name`
- `monitor.alert_threshold` / `monitor.warning_threshold` — 流量预警阈值 (百分比)

## 关键逻辑

- 流量计算需乘以 `monthly_data_multiplier` (搬瓦工 API 返回的流量倍率系数)
- 预警分四级: 🟢 <60% / 🟡 60-80% / 🟠 80-90% / 🔴 ≥90%
- 流量超标预测: 基于日均使用量线性外推至重置日
- 钉钉消息在有 🔴/🟠 级别时触发 @所有人

## 注意事项

- 所有路径在代码中硬编码为 `/opt/vps_monitor/`（日志文件、配置文件），修改部署路径需同步更新
- 日志同时输出到 `/opt/vps_monitor/monitor.log` 和 stdout
