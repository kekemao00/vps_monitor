#!/usr/bin/env python3
"""
VPS 流量监控系统 v3.0

通过搬瓦工 API 采集多台 VPS 流量数据，生成报告推送至钉钉。
"""

import argparse
import requests
import time
import hmac
import hashlib
import base64
import urllib.parse
import json
import os
import sys
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# 脚本所在目录，所有相对路径以此为基准
SCRIPT_DIR = Path(__file__).resolve().parent


def setup_logging(log_file: Path = None):
    """初始化日志，支持轮转"""
    if log_file is None:
        log_file = SCRIPT_DIR / 'monitor.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )


setup_logging()
logger = logging.getLogger(__name__)


class CronParser:
    """Cron 表达式解析器"""
    
    @staticmethod
    def get_next_run_time() -> Tuple[Optional[datetime], str]:
        """
        从 crontab 获取下次执行时间
        返回: (下次执行时间, 描述文字)
        """
        try:
            # 获取当前用户的 crontab
            result = subprocess.run(
                ['crontab', '-l'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return None, "未设置定时任务"
            
            # 查找包含 vps_monitor.py 的行
            for line in result.stdout.split('\n'):
                if 'vps_monitor.py' in line and not line.strip().startswith('#'):
                    # 解析 cron 表达式
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        minute = parts[0]
                        hour = parts[1]
                        
                        # 解析并计算下次执行时间
                        next_time, desc = CronParser._parse_cron_time(minute, hour)
                        return next_time, desc
            
            return None, "未找到监控任务"
        
        except Exception as e:
            logger.warning(f"解析 crontab 失败: {e}")
            return None, "无法获取定时信息"
    
    @staticmethod
    def _parse_cron_time(minute: str, hour: str) -> Tuple[Optional[datetime], str]:
        """
        解析 cron 时间表达式
        """
        now = datetime.now()
        
        # 解析小时
        if hour == '*':
            # 每小时
            next_hour = now.hour + 1
            if next_hour >= 24:
                next_hour = 0
                next_day = now + timedelta(days=1)
                next_time = next_day.replace(hour=next_hour, minute=int(minute) if minute.isdigit() else 0, second=0, microsecond=0)
            else:
                next_time = now.replace(hour=next_hour, minute=int(minute) if minute.isdigit() else 0, second=0, microsecond=0)
            return next_time, "每小时"
        
        elif '/' in hour:
            # 每 N 小时
            interval = int(hour.split('/')[1])
            desc = f"每 {interval} 小时"
            
            # 计算下次执行时间
            current_hour = now.hour
            next_hour = ((current_hour // interval) + 1) * interval
            
            if next_hour >= 24:
                next_hour = 0
                next_day = now + timedelta(days=1)
                next_time = next_day.replace(hour=next_hour, minute=int(minute) if minute.isdigit() else 0, second=0, microsecond=0)
            else:
                next_time = now.replace(hour=next_hour, minute=int(minute) if minute.isdigit() else 0, second=0, microsecond=0)
                if next_time <= now:
                    next_time += timedelta(hours=interval)
            
            return next_time, desc
        
        elif ',' in hour:
            # 指定多个时间点，如 9,21
            hours = [int(h) for h in hour.split(',')]
            minute_val = int(minute) if minute.isdigit() else 0
            
            # 找到下一个执行时间
            next_time = None
            for h in sorted(hours):
                candidate = now.replace(hour=h, minute=minute_val, second=0, microsecond=0)
                if candidate > now:
                    next_time = candidate
                    break
            
            # 如果今天没有了，取明天的第一个时间
            if next_time is None:
                next_day = now + timedelta(days=1)
                next_time = next_day.replace(hour=hours[0], minute=minute_val, second=0, microsecond=0)
            
            # 生成描述
            hour_desc = '、'.join([f"{h:02d}:00" for h in hours])
            desc = f"每天 {hour_desc}"
            
            return next_time, desc
        
        elif hour.isdigit():
            # 每天固定时间
            hour_val = int(hour)
            minute_val = int(minute) if minute.isdigit() else 0
            
            next_time = now.replace(hour=hour_val, minute=minute_val, second=0, microsecond=0)
            if next_time <= now:
                next_time += timedelta(days=1)
            
            desc = f"每天 {hour_val:02d}:{minute_val:02d}"
            return next_time, desc
        
        return None, "自定义时间"


class DingTalkRobot:
    """钉钉机器人类"""
    
    def __init__(self, webhook: str, secret: Optional[str] = None):
        self.webhook = webhook
        self.secret = secret
    
    def _generate_sign(self) -> Dict[str, str]:
        """生成加签参数"""
        if not self.secret:
            return {}
        
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        
        hmac_code = hmac.new(
            secret_enc,
            string_to_sign_enc,
            digestmod=hashlib.sha256
        ).digest()
        
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        
        return {
            'timestamp': timestamp,
            'sign': sign
        }
    
    def send_markdown(self, title: str, text: str, 
                     at_mobiles: List[str] = None, 
                     at_all: bool = False) -> Dict:
        """发送 Markdown 消息"""
        data = {
            'msgtype': 'markdown',
            'markdown': {
                'title': title,
                'text': text
            },
            'at': {
                'atMobiles': at_mobiles or [],
                'isAtAll': at_all
            }
        }
        
        return self._send_request(data)

    def send_alert_card(self, critical_vps: List[Dict], critical_threshold: float = 90):
        """发送独立告警消息，确保紧急信息不被长报告淹没"""
        text = f"## 🚨 流量紧急告警\n\n"
        text += f"以下服务器流量使用超过 {critical_threshold}%:\n\n"
        for v in critical_vps:
            bw = v['bandwidth']
            text += f"- **{v['name']}**: {bw['usage_percent']}% (剩余 {bw['remaining_gb']} GB，约 {bw['estimated_days_left']} 天)\n"
        text += f"\n> 请立即检查并处理"
        self.send_markdown(
            title=f'🚨 {len(critical_vps)} 台 VPS 流量紧急',
            text=text,
            at_all=True
        )

    def _send_request(self, data: Dict, retries: int = 3) -> Dict:
        """发送请求到钉钉，网络异常自动重试"""
        params = self._generate_sign()

        for attempt in range(retries):
            try:
                response = requests.post(
                    self.webhook,
                    json=data,
                    params=params,
                    timeout=10
                )
                result = response.json()

                if result.get('errcode') == 0:
                    logger.info("✅ 钉钉消息发送成功")
                else:
                    logger.error(f"❌ 钉钉消息发送失败: {result.get('errmsg')}")
                return result
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"钉钉请求失败，{wait}s 后重试: {e}")
                    time.sleep(wait)
                else:
                    logger.error(f"❌ 钉钉请求最终失败: {e}")
                    return {'errcode': -1, 'errmsg': str(e)}
            except Exception as e:
                logger.error(f"❌ 钉钉请求异常: {e}")
                return {'errcode': -1, 'errmsg': str(e)}


class BandwagonAPI:
    """搬瓦工 API 封装"""
    
    BASE_URL = "https://api.64clouds.com/v1"
    
    def __init__(self, veid: str, api_key: str):
        self.veid = veid
        self.api_key = api_key
    
    def _request(self, endpoint: str, params: Dict = None, retries: int = 3) -> Optional[Dict]:
        """通用请求方法，网络异常自动重试"""
        url = f"{self.BASE_URL}/{endpoint}"

        request_params = {
            'veid': self.veid,
            'api_key': self.api_key
        }

        if params:
            request_params.update(params)

        for attempt in range(retries):
            try:
                response = requests.get(url, params=request_params, timeout=15)
                data = response.json()

                if data.get('error') == 0:
                    return data
                else:
                    logger.error(f"API 错误 ({self.veid}): {data.get('message', '未知错误')}")
                    return None
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"API 请求失败 ({self.veid})，{wait}s 后重试: {e}")
                    time.sleep(wait)
                else:
                    logger.error(f"API 请求最终失败 ({self.veid}): {e}")
                    return None
            except Exception as e:
                logger.error(f"API 请求异常 ({self.veid}): {e}")
                return None
    
    def get_service_info(self) -> Optional[Dict]:
        """获取服务信息"""
        return self._request('getServiceInfo')

    def get_live_info(self) -> Optional[Dict]:
        """获取实时运行状态（ve_status、load_average 等）"""
        return self._request('getLiveServiceInfo')


class VPSMonitor:
    """VPS 监控核心类"""
    
    def __init__(self, robot: DingTalkRobot):
        self.robot = robot
    
    @staticmethod
    def calculate_bandwidth(data: Dict) -> Dict:
        """计算流量信息"""
        multiplier = data.get('monthly_data_multiplier', 1)
        total_bytes = data.get('plan_monthly_data', 0)
        used_bytes = data.get('data_counter', 0)

        # plan_monthly_data 是配额上限，不乘倍率；data_counter 是原始传输量，需乘倍率得到计费量
        total_gb = total_bytes / (1024**3)
        used_gb = used_bytes * multiplier / (1024**3)
        remaining_gb = max(0, total_gb - used_gb)
        usage_percent = (used_gb / total_gb * 100) if total_gb > 0 else 0

        # 计算重置时间
        reset_timestamp = data.get('data_next_reset', 0)
        reset_valid = reset_timestamp > 0
        now = datetime.now()

        if reset_valid:
            reset_date = datetime.fromtimestamp(reset_timestamp)
            days_until_reset = max(0, (reset_date - now).days)
            # 反推周期起始日（近似，搬瓦工 API 无 cycle_start 字段）
            cycle_start = reset_date - timedelta(days=30)
            days_passed = max(1, (now - cycle_start).days)
        else:
            reset_date = None
            days_until_reset = 0
            days_passed = 1

        # 日均使用量与超标预测
        daily_avg = used_gb / days_passed if days_passed > 0 else 0
        if reset_valid and days_until_reset > 0:
            predicted_usage = used_gb + (daily_avg * days_until_reset)
            will_exceed = predicted_usage > total_gb
        else:
            predicted_usage = used_gb
            will_exceed = False

        # 消耗速率分级: 实际日均 vs 理论均匀日均
        theoretical_daily = total_gb / 30 if total_gb > 0 else 1
        burn_rate = daily_avg / theoretical_daily if theoretical_daily > 0 else 0

        # 剩余可用天数估算
        estimated_days_left = int(remaining_gb / daily_avg) if daily_avg > 0 else 999

        return {
            'total_gb': round(total_gb, 2),
            'used_gb': round(used_gb, 2),
            'remaining_gb': round(remaining_gb, 2),
            'usage_percent': round(usage_percent, 2),
            'reset_date': reset_date.strftime('%Y-%m-%d') if reset_date else '未知',
            'reset_datetime': reset_date.strftime('%Y-%m-%d %H:%M') if reset_date else '未知',
            'days_until_reset': days_until_reset,
            'reset_valid': reset_valid,
            'daily_avg': round(daily_avg, 2),
            'predicted_usage': round(predicted_usage, 2),
            'will_exceed': will_exceed,
            'burn_rate': round(burn_rate, 2),
            'estimated_days_left': estimated_days_left
        }
    
    @staticmethod
    def get_status_icon(usage_percent: float, critical_bound: float = 90, alert_bound: float = 80, warning_bound: float = 60) -> str:
        """根据使用率和配置阈值返回状态图标"""
        if usage_percent >= critical_bound:
            return '🔴'
        elif usage_percent >= alert_bound:
            return '🟠'
        elif usage_percent >= warning_bound:
            return '🟡'
        else:
            return '🟢'
    
    @staticmethod
    def get_progress_bar(percent: float, length: int = 20) -> str:
        """生成高精度进度条"""
        filled = int(percent / 100 * length)
        remainder = (percent / 100 * length) - filled
        bar = '█' * filled
        if remainder >= 0.5 and filled < length:
            bar += '▓'
            filled += 1
        bar += '░' * (length - filled)
        return f"`{bar}` {percent}%"
    
    @staticmethod
    def format_bytes(bytes_value: int) -> str:
        """格式化字节数"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.2f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.2f} PB"

    @staticmethod
    def get_ve_status_icon(ve_status: str, suspended: bool) -> str:
        """VPS 运行状态图标"""
        if suspended:
            return '⛔ 已暂停'
        status_map = {
            'running': '🟢 运行中',
            'stopped': '🔴 已停止',
            'suspended': '⛔ 已暂停',
        }
        return status_map.get(ve_status, '⚪ 未知')

    @staticmethod
    def get_trend_arrow(current_gb: float, last_gb: Optional[float]) -> str:
        """根据历史对比生成趋势箭头"""
        if last_gb is None:
            return ''
        diff = current_gb - last_gb
        if diff > 0.5:
            return f' ↑+{diff:.1f}GB'
        elif diff > 0.1:
            return f' ↗+{diff:.1f}GB'
        elif diff > -0.1:
            return ' →'
        else:
            return f' ↘{diff:.1f}GB'

    @staticmethod
    def get_burn_rate_label(burn_rate: float) -> str:
        """消耗速率分级标签"""
        if burn_rate > 1.5:
            return '⚡消耗偏快'
        elif burn_rate < 0.5:
            return '🐢消耗很低'
        return ''
    
    def check_single_vps(self, veid: str, api_key: str, name: str = None) -> Optional[Dict]:
        """检查单个 VPS，合并服务信息和实时状态"""
        api = BandwagonAPI(veid, api_key)
        data = api.get_service_info()

        if not data:
            return None

        bandwidth = self.calculate_bandwidth(data)

        # 获取实时运行状态（失败不影响主流程）
        live_data = api.get_live_info()
        ve_status = 'unknown'
        if live_data:
            ve_status = live_data.get('ve_status', 'unknown')

        return {
            'veid': veid,
            'name': name or data.get('hostname', veid),
            'hostname': data.get('hostname', 'N/A'),
            'location': data.get('node_location', 'N/A'),
            'plan': data.get('plan', 'N/A'),
            'plan_disk': self.format_bytes(data.get('plan_disk', 0)),
            'plan_ram': self.format_bytes(data.get('plan_ram', 0)),
            'ip_addresses': data.get('ip_addresses', []),
            'bandwidth': bandwidth,
            'os': data.get('os', 'N/A'),
            'suspended': data.get('suspended', False),
            've_status': ve_status
        }
    
    def check_vps_list(self, vps_configs: List[Dict]) -> Tuple[List[Dict], List[str]]:
        """并发批量检查 VPS 列表，返回 (成功结果, 失败名称)"""
        results = []
        failed = []

        logger.info(f"开始检查 {len(vps_configs)} 台 VPS...")

        with ThreadPoolExecutor(max_workers=min(len(vps_configs), 5)) as executor:
            futures = {
                executor.submit(
                    self.check_single_vps,
                    c['veid'], c['api_key'], c.get('name')
                ): c
                for c in vps_configs
            }
            for future in as_completed(futures):
                config = futures[future]
                name = config.get('name', config['veid'])
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                        logger.info(f"  ✅ {name} - 使用率: {result['bandwidth']['usage_percent']}%")
                    else:
                        failed.append(name)
                        logger.warning(f"  ❌ {name} - 查询失败")
                except Exception as e:
                    failed.append(name)
                    logger.error(f"  ❌ {name} - 异常: {e}")

        logger.info(f"检查完成: 成功 {len(results)}/{len(vps_configs)}")
        return results, failed
    
    def send_detailed_report(self, vps_list: List[Dict], critical_threshold: float = 90, alert_threshold: float = 80, warning_threshold: float = 60, failed_names: List[str] = None):
        """发送详细报告（分层展示 + 历史对比 + 动态总结）"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        next_run_time, next_run_desc = CronParser.get_next_run_time()

        # 加载历史数据用于趋势对比
        last_history = load_last_history()
        last_map = {}
        if last_history:
            for v in last_history.get('vps', []):
                last_map[v['name']] = v.get('used_gb')

        # 四级分组，边界完全由配置驱动
        critical_bound = critical_threshold
        alert_bound = alert_threshold
        warning_bound = warning_threshold

        # ==================== 分级统计 ====================
        total_count = len(vps_list)
        critical = [v for v in vps_list if v['bandwidth']['usage_percent'] >= critical_bound]
        alert    = [v for v in vps_list if alert_bound <= v['bandwidth']['usage_percent'] < critical_bound]
        warning  = [v for v in vps_list if warning_bound <= v['bandwidth']['usage_percent'] < alert_bound]
        normal   = [v for v in vps_list if v['bandwidth']['usage_percent'] < warning_bound]

        total_bandwidth = sum(v['bandwidth']['total_gb'] for v in vps_list)
        total_used = sum(v['bandwidth']['used_gb'] for v in vps_list)
        total_remaining = sum(v['bandwidth']['remaining_gb'] for v in vps_list)
        overall_usage = (total_used / total_bandwidth * 100) if total_bandwidth > 0 else 0
        will_exceed = [v for v in vps_list if v['bandwidth']['will_exceed']]

        # ==================== 标题 ====================
        if critical:
            title = f'🔴 VPS 流量严重预警 ({len(critical)}台)'
            status_emoji = '🔴'
        elif alert:
            title = f'🟠 VPS 流量预警 ({len(alert)}台)'
            status_emoji = '🟠'
        elif warning:
            title = f'🟡 VPS 流量警告 ({len(warning)}台)'
            status_emoji = '🟡'
        else:
            title = f'✅ VPS 流量正常 ({total_count}台)'
            status_emoji = '✅'

        text = f"## {status_emoji} VPS 流量监控报告\n\n"
        text += f"**检查时间**: {now}\n\n"
        text += f"**阈值**: 🔴≥{critical_bound}% | 🟠≥{alert_bound}% | 🟡≥{warning_bound}%\n\n"
        text += "---\n\n"

        # ==================== 紧凑型总览 ====================
        text += f"📊 **{total_count}台** | "
        text += f"总量 {total_bandwidth:.1f} GB | "
        text += f"已用 {total_used:.1f} GB ({overall_usage:.1f}%) | "
        text += f"剩余 {total_remaining:.1f} GB\n\n"

        # 状态分布（一行）
        dist_parts = []
        if critical:
            dist_parts.append(f'🔴×{len(critical)}')
        if alert:
            dist_parts.append(f'🟠×{len(alert)}')
        if warning:
            dist_parts.append(f'🟡×{len(warning)}')
        dist_parts.append(f'🟢×{len(normal)}')
        text += f"**状态**: {' '.join(dist_parts)}\n\n"

        if will_exceed:
            text += f"⚠️ **预测超标**: {len(will_exceed)} 台可能在重置前超出配额\n\n"
        if failed_names:
            text += f"❌ **查询失败**: {', '.join(failed_names)}（请检查 API 密钥或网络）\n\n"

        text += "---\n\n"

        # ==================== VPS 详细列表 ====================
        sorted_vps = sorted(vps_list, key=lambda x: x['bandwidth']['usage_percent'], reverse=True)

        # 需要展开详情的 VPS（>=warning_bound 或有异常状态）
        detail_vps = [v for v in sorted_vps if v['bandwidth']['usage_percent'] >= warning_bound or v['suspended'] or v.get('ve_status') == 'stopped']
        summary_vps = [v for v in sorted_vps if v not in detail_vps]

        # 展开详情的 VPS
        if detail_vps:
            text += f"### 📋 需关注 ({len(detail_vps)}台)\n\n"
            for idx, vps in enumerate(detail_vps, 1):
                bw = vps['bandwidth']
                icon = self.get_status_icon(bw['usage_percent'], critical_bound, alert_bound, warning_bound)
                ve_label = self.get_ve_status_icon(vps.get('ve_status', 'unknown'), vps['suspended'])
                trend = self.get_trend_arrow(bw['used_gb'], last_map.get(vps['name']))
                burn_label = self.get_burn_rate_label(bw.get('burn_rate', 0))

                text += f"#### {icon} {idx}. {vps['name']} [{ve_label}]\n\n"
                text += f"{self.get_progress_bar(bw['usage_percent'])}\n\n"
                text += f"- 已用: **{bw['used_gb']}/{bw['total_gb']} GB** ({bw['usage_percent']}%){trend}\n"
                text += f"- 剩余: **{bw['remaining_gb']} GB**"
                if bw.get('estimated_days_left', 999) < 999:
                    text += f" (约 **{bw['estimated_days_left']} 天**)"
                text += "\n"

                if bw.get('reset_valid', True):
                    text += f"- 重置: {bw['reset_datetime']} ({bw['days_until_reset']} 天后)\n"
                else:
                    text += f"- 重置: 未知\n"

                if bw['daily_avg'] > 0 and bw.get('reset_valid', True):
                    text += f"- 日均: {bw['daily_avg']} GB"
                    if burn_label:
                        text += f" {burn_label}"
                    text += "\n"
                    if bw['will_exceed']:
                        text += f"- ⚠️ 预计使用 {bw['predicted_usage']} GB，**将超标**\n"

                text += f"- 位置: {vps['location']} | 套餐: {vps['plan']}\n"

                if bw['usage_percent'] >= critical_bound:
                    text += f"- 🚨 **紧急: 流量即将耗尽**\n"
                elif bw['usage_percent'] >= warning_bound:
                    text += f"- ⚠️ **警告: 建议清理或升级**\n"

                text += "\n---\n\n"

        # 正常 VPS 折叠为摘要
        if summary_vps:
            text += f"### 🟢 运行正常 ({len(summary_vps)}台)\n\n"
            for vps in summary_vps:
                bw = vps['bandwidth']
                ve_label = self.get_ve_status_icon(vps.get('ve_status', 'unknown'), vps['suspended'])
                trend = self.get_trend_arrow(bw['used_gb'], last_map.get(vps['name']))
                days_info = f" | 重置 {bw['days_until_reset']}天后" if bw.get('reset_valid', True) else ""
                text += f"- {vps['name']} [{ve_label}]: "
                text += f"**{bw['used_gb']}/{bw['total_gb']} GB** ({bw['usage_percent']}%)"
                text += f"{trend}{days_info}\n"
            text += "\n---\n\n"

        # ==================== 动态一句话总结 + 下次检查 ====================
        worst = sorted_vps[0] if sorted_vps else None
        if critical:
            summary_line = f"⚠️ {critical[0]['name']} 使用率 {critical[0]['bandwidth']['usage_percent']}%，剩余约 {critical[0]['bandwidth']['estimated_days_left']} 天，请及时处理"
        elif alert:
            summary_line = f"⚠️ {alert[0]['name']} 使用率 {alert[0]['bandwidth']['usage_percent']}%，建议关注"
        elif worst:
            summary_line = f"✅ 全部正常，最高使用率 {worst['name']} {worst['bandwidth']['usage_percent']}%"
        else:
            summary_line = "✅ 全部正常"

        text += f"> {summary_line}\n"
        if next_run_time:
            time_diff = next_run_time - datetime.now()
            hours = int(time_diff.total_seconds() / 3600)
            minutes = int((time_diff.total_seconds() % 3600) / 60)
            time_until = f"{hours}h{minutes}m" if hours > 0 else f"{minutes}m"
            text += f"> ⏰ 下次检查: {next_run_time.strftime('%H:%M')} ({time_until}后) | {next_run_desc}\n"
        else:
            text += f"> 📅 {next_run_desc}\n"

        # ==================== 发送 ====================
        at_all = any(v['bandwidth']['usage_percent'] >= alert_bound for v in vps_list)
        self.robot.send_markdown(title=title, text=text, at_all=at_all)

        # 🔴 级别额外发送独立紧急告警消息
        if critical:
            self.robot.send_alert_card(critical, critical_bound)
    
    def monitor_and_report(self, vps_configs: List[Dict], critical_threshold: float = 90, alert_threshold: float = 80, warning_threshold: float = 60):
        """监控并发送详细报告"""
        results, failed = self.check_vps_list(vps_configs)

        if not results and not failed:
            logger.error("❌ 没有获取到任何 VPS 数据")
            return

        if results:
            self.send_detailed_report(results, critical_threshold, alert_threshold, warning_threshold, failed_names=failed)
            save_history(results)
        else:
            logger.error(f"❌ 所有 VPS 查询失败: {failed}")
            self.robot.send_markdown(
                title='❌ VPS 监控异常',
                text=f"## ❌ 监控执行失败\n\n所有 VPS 查询均失败，请检查网络或 API 密钥。\n\n**失败列表**: {', '.join(failed)}\n\n**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                at_all=True
            )
            return

        alerts = [v for v in results if v['bandwidth']['usage_percent'] >= alert_threshold]
        if alerts:
            logger.warning(f"⚠️  发现 {len(alerts)} 台 VPS 超过阈值 {alert_threshold}%")
        else:
            logger.info(f"✅ 所有 VPS 流量正常（低于 {alert_threshold}%）")


def load_config(config_file: str = None) -> Dict:
    """加载并校验配置文件，敏感字段支持环境变量覆盖"""
    if config_file is None:
        config_file = str(SCRIPT_DIR / 'config.json')

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"✅ 配置文件加载成功: {config_file}")
    except FileNotFoundError:
        logger.error(f"❌ 配置文件不存在: {config_file}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"❌ 配置文件格式错误: {e}")
        sys.exit(1)

    # 配置 schema 校验
    errors = []
    if 'dingtalk' not in config or not isinstance(config.get('dingtalk'), dict):
        errors.append("缺少 dingtalk 配置段")
    elif 'webhook' not in config['dingtalk']:
        errors.append("缺少 dingtalk.webhook")
    if 'vps_list' not in config or not isinstance(config.get('vps_list'), list):
        errors.append("缺少 vps_list 或格式不是数组")
    elif len(config['vps_list']) == 0:
        errors.append("vps_list 不能为空")
    else:
        for i, vps in enumerate(config['vps_list']):
            if not isinstance(vps, dict):
                errors.append(f"vps_list[{i}] 必须是对象，实际是 {type(vps).__name__}")
            elif 'veid' not in vps or 'api_key' not in vps:
                errors.append(f"vps_list[{i}] 缺少 veid 或 api_key")
    if 'monitor' not in config or not isinstance(config.get('monitor'), dict):
        errors.append("缺少 monitor 配置段")
    elif 'alert_threshold' not in config['monitor']:
        errors.append("缺少 monitor.alert_threshold")
    else:
        alert_t = config['monitor']['alert_threshold']
        warning_t = config['monitor'].get('warning_threshold', 60)
        critical_t = config['monitor'].get('critical_threshold', 90)
        # bool 是 int 子类，需显式排除
        def _is_number(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        if not _is_number(alert_t):
            errors.append("monitor.alert_threshold 必须是数字（非布尔）")
        elif not _is_number(warning_t):
            errors.append("monitor.warning_threshold 必须是数字（非布尔）")
        elif not _is_number(critical_t):
            errors.append("monitor.critical_threshold 必须是数字（非布尔）")
        elif not (0 <= warning_t < alert_t < critical_t <= 100):
            errors.append(f"阈值顺序错误: warning({warning_t}) < alert({alert_t}) < critical({critical_t}) 必须成立")

    if errors:
        for e in errors:
            logger.error(f"❌ 配置校验失败: {e}")
        sys.exit(1)

    # 环境变量覆盖敏感字段
    config['dingtalk']['webhook'] = os.environ.get('DINGTALK_WEBHOOK', config['dingtalk']['webhook'])
    if 'secret' in config['dingtalk']:
        config['dingtalk']['secret'] = os.environ.get('DINGTALK_SECRET', config['dingtalk']['secret'])
    for vps in config['vps_list']:
        env_key = f"VPS_{vps['veid']}_API_KEY"
        vps['api_key'] = os.environ.get(env_key, vps['api_key'])

    return config


def load_last_history() -> Optional[Dict]:
    """读取上次执行记录，用于趋势对比"""
    history_file = SCRIPT_DIR / 'history.jsonl'
    if not history_file.exists():
        return None
    last_line = None
    try:
        with open(history_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    last_line = line
        return json.loads(last_line) if last_line else None
    except Exception:
        return None


def save_history(results: List[Dict]):
    """将执行摘要追加写入 history.jsonl，用于趋势分析"""
    record = {
        'timestamp': datetime.now().isoformat(),
        'vps': [{
            'name': r['name'],
            'usage_percent': r['bandwidth']['usage_percent'],
            'used_gb': r['bandwidth']['used_gb']
        } for r in results]
    }
    try:
        with open(SCRIPT_DIR / 'history.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.warning(f"历史记录写入失败: {e}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='VPS 流量监控系统')
    parser.add_argument('--config', '-c', type=str, default=None,
                        help='配置文件路径（默认: 脚本目录下 config.json）')
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("🚀 VPS 流量监控系统启动")
    logger.info("=" * 50)

    # 加载配置
    config = load_config(args.config)

    # 初始化钉钉机器人
    robot = DingTalkRobot(
        webhook=config['dingtalk']['webhook'],
        secret=config['dingtalk'].get('secret')
    )

    # 初始化监控器
    monitor = VPSMonitor(robot)

    # 执行监控并发送详细报告
    monitor.monitor_and_report(
        vps_configs=config['vps_list'],
        critical_threshold=config['monitor'].get('critical_threshold', 90),
        alert_threshold=config['monitor']['alert_threshold'],
        warning_threshold=config['monitor'].get('warning_threshold', 60)
    )

    logger.info("=" * 50)
    logger.info("✅ 监控任务完成")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
