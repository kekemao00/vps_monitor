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
- **群内 @机器人 查询** — 发「状态」「查询 名称」即时回复，Stream 模式无需公网 IP
- **两种部署方式** — 服务器 crontab，或免服务器的 GitHub Actions（外部准点触发 + 兜底定时 + 按时段去重）

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

## 部署方式

两种方式任选其一，功能一样，**不要同时启用**（两边的推送记录不互通，会重复推送）：

| | 方式一：服务器 crontab | 方式二：GitHub Actions |
|---|---|---|
| 需要什么 | 一台常开的服务器 | 只要 GitHub 仓库，不需要服务器 |
| 准点程度 | 准点 | 配外部触发后通常 1~2 分钟内送达；只用 GitHub 自带定时会延迟几十分钟到几小时 |
| 历史记录 | 服务器上的 `history.jsonl` | 仓库的 `state` 分支 |
| @机器人 查询 | 可以同机常驻 | Actions 跑不了常驻进程，需要另找一台常开的机器跑 `dingtalk_bot.py` |

### 方式一：服务器 crontab

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

### 方式二：GitHub Actions

工作流在 [`.github/workflows/report.yml`](.github/workflows/report.yml)，默认每天北京时间 09:00 和 21:00 两个**报告时段**。

#### 怎么保证准点

GitHub 自带的定时任务（`schedule`）不保证准点，高峰期常常延迟几十分钟到几小时，偶尔直接丢弃不补跑。所以这里用三层保障：

1. **准点触发**：外部定时服务（推荐免费的 [cron-job.org](https://cron-job.org)）在 09:00 / 21:00 调用 GitHub 接口启动工作流，通常 1 分钟内开始运行。
2. **兜底触发**：GitHub 自带的定时任务在每个时段之后再触发 3 次（09:17、10:07、11:07 和 21:17、22:07、23:07），外部服务出问题时也不会漏推。
3. **按时段去重**：每次推送成功后在 `state` 分支记下「这个时段已推送」，同一时段后到的触发直接跳过；推送失败不记，由下一路触发重试。兜底触发如果拖到时段之后 3 小时以上就不再推送（留给下一个时段，避免深夜打扰）。

只做第 2 步也能用，只是可能晚到。

#### 第 1 步：填写 Secret

仓库 Settings → Secrets and variables → Actions → **Secrets** → New repository secret：

| 名称 | 是否必填 | 内容 |
|---|---|---|
| `VPS_MONITOR_CONFIG` | 必填 | 完整的 `config.json` 内容（直接粘贴整个 JSON） |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | 可选 | 单独存放的钉钉凭据，会覆盖上面 JSON 里的值 |

> 钉钉机器人的安全设置用「加签」，**不要勾选 IP 地址段**：GitHub Actions 的出口 IP 不固定。

#### 第 2 步：先预览一次

Actions → **VPS 流量报告** → Run workflow，保持勾选「只生成预览」。运行结束后在这次运行的 Summary 页面能看到报告内容，不会发消息。

再运行一次并**取消勾选**「只生成预览」，群里会收到报告，仓库里会多出一个 `state` 分支存历史记录。

#### 第 3 步：打开定时

Settings → Secrets and variables → Actions → **Variables** → New repository variable：名称 `VPS_MONITOR_ACTIONS`，值 `true`。

没有这个变量时 GitHub 自带的定时任务不会运行，这样合并代码后不会和服务器部署重复推送。从服务器迁移过来时，记得删掉服务器上的 crontab 行；想保留趋势对比，可以把服务器上的 `history.jsonl` 提交到 `state` 分支。

#### 第 4 步：准点触发（推荐）

**建一个只能触发 Actions 的 token**：GitHub 头像 → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token：

| 选项 | 填什么 |
|---|---|
| Repository access | Only select repositories，只选这个仓库 |
| Permissions | Repository permissions 里把 **Actions** 设为 **Read and write**，其他都不加 |
| Expiration | 一年；过期期间由 GitHub 自带定时兜底，换新 token 即可 |

这个 token 只能启动和查看本仓库的工作流，不能改代码、读不到 Secrets。

**在 cron-job.org 建定时任务**（注册登录后 CREATE CRONJOB）：

- URL：`https://api.github.com/repos/<用户名>/<仓库名>/actions/workflows/report.yml/dispatches`
- 执行时间：每天 09:00 和 21:00，时区选 `Asia/Shanghai`；打开失败通知
- ADVANCED → Request method：`POST`
- Headers：

  | Key | Value |
  |---|---|
  | `Accept` | `application/vnd.github+json` |
  | `Authorization` | `Bearer github_pat_…`（`Bearer`、一个空格、再接 token） |
  | `X-GitHub-Api-Version` | `2022-11-28` |
  | `Content-Type` | `application/json` |

- Request body：

  ```json
  {"ref": "main", "inputs": {"dry_run": "false", "scheduled": "true"}}
  ```

  `scheduled` 让这次运行按时段去重，外部服务重试或和兜底触发撞在一起也不会重复推送。

保存后点 **TEST RUN**，返回 `204` 即成功；`401` 多半是漏了 `Bearer `，`403` 是 token 没有 Actions 写权限，`404` 是 URL 里的用户名/仓库名不对或 token 没选这个仓库，`422` 是 body 格式不对或工作流被停用。

> 也可以用其他能发带 Header 的 POST 请求的定时服务，比如 Cloudflare Workers 的 Cron Triggers，做法相同。

#### 修改报告时段

三处保持一致：`report.yml` 里的 `VPS_MONITOR_SLOTS`（如 `"08:30,20:30"`）、兜底的 `cron`（UTC 时间，等于北京时间减 8 小时，排在时段之后）、cron-job.org 的执行时间。

#### 注意

- 仓库是**公开**的话，`state` 分支里的历史记录（VPS 名称、已用流量）任何人都能看到；介意的话把仓库改为私有。私有仓库会占用 Actions 免费额度：每个时段推送一次约 2 分钟（两个任务各按 1 分钟计），兜底触发跳过时各约 1 分钟，一个月约 300 分钟，在免费账号每月 2000 分钟之内。
- 公开仓库连续 60 天没有活动时，GitHub 会自动停用定时任务（外部触发会返回 `422`），到 Actions 页面重新启用即可。
- 同样的时段去重也能在服务器上用：`python3 vps_monitor.py --scheduled --slots 09:00,21:00`，一般用不到。

## 群内 @机器人 查询（可选）

在群里 @机器人 发指令即时查询，不用等定时报告：

| 指令 | 作用 |
|------|------|
| `状态` / `status` | 查询全部 VPS，内容同定时报告（不写历史，不影响「较上次」） |
| `查询 名称` / `query 名称` | 单台 VPS 详情，名称可写 veid 或名称片段 |
| `列表` / `list` | 列出已配置的 VPS |
| `帮助` / `help` | 显示指令说明 |

定时报告用的「自定义机器人」（webhook）只能发不能收，所以 @ 查询需要另建一个**企业内部应用机器人**，用 Stream 模式接收消息：程序主动连钉钉，**不需要公网 IP、域名或回调地址**。两个机器人可以放在同一个群里。

### 1. 创建机器人

1. 登录 [钉钉开放平台](https://open-dev.dingtalk.com/)（需要是企业/团队的管理员或开发者，个人可免费创建一个团队）
2. 应用开发 → 企业内部应用 → 创建应用
3. 应用能力 → 添加「机器人」，消息接收模式选 **Stream 模式**，保存并发布
4. 凭证与基础信息 → 复制 **Client ID** 和 **Client Secret**
5. 版本管理与发布 → 发布应用
6. 在目标群：群设置 → 机器人 → 添加机器人 → 选择刚创建的应用机器人

### 2. 配置并启动

在 `config.json` 加入（也可用环境变量 `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET`）：

```json
"dingtalk_bot": {
  "client_id": "dingxxxxxxxx",
  "client_secret": "xxxxxxxx"
}
```

```bash
pip install -r requirements.txt

# 先在终端试指令，不连钉钉
python3 dingtalk_bot.py --cli

# 正式运行（常驻进程）
python3 dingtalk_bot.py
```

### 3. 用 systemd 常驻

```ini
# /etc/systemd/system/vps-monitor-bot.service
[Unit]
Description=VPS Monitor DingTalk Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/vps_monitor
ExecStart=/usr/bin/python3 /opt/vps_monitor/dingtalk_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vps-monitor-bot
journalctl -u vps-monitor-bot -f   # 或查看 bot.log
```

> 指令都是只读查询；企业内部机器人只有本企业/团队成员能 @ 到或私聊。
>
> 定时报告用 GitHub Actions 部署时，指令服务仍需一台常开的机器（Stream 模式不需要公网 IP，家里的 NAS、树莓派也行）；这时它的「较上次」对比读的是本机的 `history.jsonl`，不是 `state` 分支。

## 项目结构

```
vps_monitor/
├── vps_monitor.py          # 主程序（定时报告）
├── dingtalk_bot.py         # @机器人 指令服务（可选，常驻）
├── config.json             # 配置文件（含敏感信息，已 gitignore）
├── config.example.json     # 配置示例
├── requirements.txt        # Python 依赖
├── tests/                  # 单元测试
├── .github/workflows/      # report.yml 定时报告（Actions 部署）、ci.yml 单元测试
├── scripts/save_state.sh   # Actions 部署时把历史记录提交到 state 分支
├── history.jsonl           # 执行历史（自动生成）
├── monitor.log             # 运行日志（自动轮转，5MB x 3）
├── bot.log                 # 指令服务日志
├── LICENSE                 # MIT 开源协议
└── README.md               # 本文件
```

## 开源协议

[MIT License](LICENSE)
