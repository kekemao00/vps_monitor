# VPS 流量监控系统

> 搬瓦工 (BandwagonHost) VPS 流量自动监控，定时采集、智能预警、钉钉推送。

![Python](https://img.shields.io/badge/Python-3.7+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

## 功能特性

- **四级预警** — 🟢正常 / 🟡警告 / 🟠预警 / 🔴严重，阈值完全可配置
- **流量趋势预测** — 基于日均消耗线性外推，提前预警超标风险
- **历史对比** — 自动对比上次执行数据，展示趋势箭头 (↑↗→↘)
- **消耗速率分级** — ⚡消耗偏快 / 🐢消耗很低，一眼识别异常
- **剩余天数估算** — 直观展示"还能用多少天"
- **VPS 实时状态** — 调用 `getLiveServiceInfo` 获取运行/停止/暂停状态
- **分层报告** — 正常 VPS 折叠摘要，异常 VPS 自动展开详情
- **独立紧急告警** — 🔴级别额外发送独立消息，确保不被长报告淹没
- **并发查询** — 多台 VPS 并行采集，大幅缩短执行时间
- **自动重试** — API 和钉钉发送均支持指数退避重试
- **环境变量覆盖** — 敏感信息支持从环境变量读取，安全部署

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/YOUR_USERNAME/vps-monitor.git
cd vps-monitor
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

复制示例配置并填入实际信息：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "dingtalk": {
    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN",
    "secret": "SEC_YOUR_SECRET"
  },
  "vps_list": [
    {
      "veid": "YOUR_VEID",
      "api_key": "YOUR_API_KEY",
      "name": "my-vps"
    }
  ],
  "monitor": {
    "critical_threshold": 90,
    "alert_threshold": 80,
    "warning_threshold": 60
  }
}
```

> `veid` 和 `api_key` 在搬瓦工 KiwiVM 控制面板 → API 页面获取。

### 4. 运行

```bash
python3 vps_monitor.py
```

指定配置文件：

```bash
python3 vps_monitor.py --config /path/to/config.json
```

预览消息内容（只打印到终端，不发送钉钉、不写历史记录）：

```bash
python3 vps_monitor.py --dry-run
```

运行单元测试：

```bash
python3 -m unittest discover -s tests
```

## 配置说明

### 阈值配置

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `critical_threshold` | 90 | 🔴 严重预警阈值，触发独立告警消息 |
| `alert_threshold` | 80 | 🟠 预警阈值，触发 @所有人（有 🔴 时只由紧急告警 @ 一次） |
| `warning_threshold` | 60 | 🟡 警告阈值，报告中展开详情 |

约束：`warning_threshold < alert_threshold < critical_threshold`

### 环境变量覆盖

敏感信息可通过环境变量覆盖 `config.json` 中的值：

| 环境变量 | 覆盖字段 |
|----------|----------|
| `DINGTALK_WEBHOOK` | `dingtalk.webhook` |
| `DINGTALK_SECRET` | `dingtalk.secret` |
| `VPS_{veid}_API_KEY` | 对应 VPS 的 `api_key` |

示例：

```bash
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxx"
export VPS_2052102_API_KEY="private_xxx"
```

## 定时任务部署

推荐使用 crontab 定时执行：

```bash
crontab -e

# 每天 9:00 和 21:00 执行
0 9,21 * * * /usr/bin/python3 /opt/vps_monitor/vps_monitor.py >> /opt/vps_monitor/cron.log 2>&1
```

确认 cron 服务运行中：

```bash
sudo systemctl status cron
```

## 项目结构

```
vps_monitor/
├── vps_monitor.py          # 主程序
├── config.json             # 配置文件（含敏感信息，已 gitignore）
├── config.example.json     # 配置示例
├── requirements.txt        # Python 依赖
├── tests/                  # 单元测试
├── history.jsonl           # 执行历史（自动生成）
├── monitor.log             # 运行日志（自动轮转，5MB x 3）
├── LICENSE                 # MIT 开源协议
└── README.md               # 本文件
```

## 开源协议

[MIT License](LICENSE)
