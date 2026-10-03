#!/usr/bin/env python3
"""
VPS 流量监控系统 v3.1

通过搬瓦工 API 采集多台 VPS 流量数据，生成报告推送至钉钉。
"""

import argparse
import requests
import time
import hmac
import hashlib
import base64
import calendar
import re
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
from typing import List, Dict, Optional, Tuple, Set

# 脚本所在目录，所有相对路径以此为基准
SCRIPT_DIR = Path(__file__).resolve().parent

# 钉钉 Markdown 消息体上限约 20000 字节，留出余量
DINGTALK_MAX_BYTES = 18000

logger = logging.getLogger(__name__)


def setup_logging(log_file: Path = None):
    """初始化日志，支持轮转；日志目录不可写时退化为仅输出到控制台"""
    if log_file is None:
        log_file = SCRIPT_DIR / 'monitor.log'
    handlers = [logging.StreamHandler(sys.stdout)]
    file_error = None
    try:
        handlers.insert(0, RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'))
    except OSError as e:
        file_error = e
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers
    )
    if file_error:
        logger.warning(f"日志文件不可写，仅输出到控制台: {file_error}")


def redact(text: str) -> str:
    """隐藏异常信息中 URL 携带的密钥（api_key / access_token / sign）"""
    return re.sub(r'(api_key|access_token|sign)=[^&\s\'")]+', r'\1=***', str(text))


def fmt_pct(value: float) -> str:
    """百分比统一保留 1 位小数"""
    return f"{value:.1f}%"


def fmt_gb(value: float) -> str:
    """流量统一保留 1 位小数"""
    return f"{value:.1f} GB"


def fmt_duration(seconds: float) -> str:
    """把秒数转成「x 天」/「x 小时」/「x 分钟」"""
    seconds = max(0, seconds)
    if seconds >= 86400:
        return f"{int(seconds // 86400)} 天"
    if seconds >= 3600:
        return f"{int(seconds // 3600)} 小时"
    return f"{max(1, int(seconds // 60))} 分钟"


class CronParser:
    """Cron 表达式解析器（支持 * , - / 及 @daily 等宏）"""

    MACROS = {
        '@yearly': '0 0 1 1 *',
        '@annually': '0 0 1 1 *',
        '@monthly': '0 0 1 * *',
        '@weekly': '0 0 * * 0',
        '@daily': '0 0 * * *',
        '@midnight': '0 0 * * *',
        '@hourly': '0 * * * *',
    }
    WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六']

    @staticmethod
    def get_next_run_time() -> Tuple[Optional[datetime], str]:
        """
        从 crontab 获取下次执行时间
        返回: (下次执行时间, 描述文字)；未找到任务时时间为 None
        """
        try:
            result = subprocess.run(
                ['crontab', '-l'],
                capture_output=True,
                text=True,
                timeout=5
            )
        except Exception as e:
            logger.warning(f"读取 crontab 失败: {e}")
            return None, ''

        if result.returncode != 0:
            return None, ''

        best: Tuple[Optional[datetime], str] = (None, '')
        for line in result.stdout.split('\n'):
            line = line.strip()
            if 'vps_monitor.py' not in line or line.startswith('#'):
                continue
            parts = line.split()
            if parts[0] in CronParser.MACROS:
                fields = CronParser.MACROS[parts[0]].split()
            elif len(parts) >= 5:
                fields = parts[:5]
            else:
                continue
            next_time, desc = CronParser.parse(*fields)
            if next_time and (best[0] is None or next_time < best[0]):
                best = (next_time, desc)
        return best

    @staticmethod
    def _parse_field(expr: str, lo: int, hi: int) -> Set[int]:
        """解析单个 cron 字段为取值集合，非法时抛出 ValueError"""
        values = set()
        for part in expr.split(','):
            step = 1
            if '/' in part:
                part, step_str = part.split('/', 1)
                step = int(step_str)
                if step <= 0:
                    raise ValueError(f"非法步长: {expr}")
            if part == '*':
                start, end = lo, hi
            elif '-' in part:
                a, b = part.split('-', 1)
                start, end = int(a), int(b)
            else:
                start = int(part)
                # 形如 5/15 表示从 5 开始每 15
                end = hi if step > 1 else start
            if start < lo or end > hi or start > end:
                raise ValueError(f"字段越界: {expr}")
            values.update(range(start, end + 1, step))
        return values

    @staticmethod
    def parse(minute: str, hour: str, dom: str = '*', month: str = '*', dow: str = '*',
              now: datetime = None) -> Tuple[Optional[datetime], str]:
        """计算 cron 表达式的下次执行时间与可读描述"""
        now = now or datetime.now()
        try:
            minutes = sorted(CronParser._parse_field(minute, 0, 59))
            hours = sorted(CronParser._parse_field(hour, 0, 23))
            doms = CronParser._parse_field(dom, 1, 31)
            months = CronParser._parse_field(month, 1, 12)
            dows = {d % 7 for d in CronParser._parse_field(dow, 0, 7)}
        except ValueError:
            return None, f"自定义周期 ({minute} {hour} {dom} {month} {dow})"

        dom_any, dow_any = dom == '*', dow == '*'

        def day_matches(d: datetime) -> bool:
            if d.month not in months:
                return False
            cron_dow = (d.weekday() + 1) % 7
            if dom_any and dow_any:
                return True
            if dom_any:
                return cron_dow in dows
            if dow_any:
                return d.day in doms
            # 两者都受限时，cron 语义为「任一满足」
            return d.day in doms or cron_dow in dows

        start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        next_time = None
        day = start.replace(hour=0, minute=0)
        for _ in range(366 * 5):
            if day_matches(day):
                for h in hours:
                    for m in minutes:
                        candidate = day.replace(hour=h, minute=m)
                        if candidate >= start:
                            next_time = candidate
                            break
                    if next_time:
                        break
            if next_time:
                break
            day += timedelta(days=1)

        return next_time, CronParser._describe(minute, hour, dom, month, dow, minutes, hours, dows)

    @staticmethod
    def _describe(minute, hour, dom, month, dow, minutes, hours, dows) -> str:
        """生成人类可读的执行周期描述"""
        if dom != '*' or month != '*':
            return f"自定义周期 ({minute} {hour} {dom} {month} {dow})"

        if dow == '*':
            prefix = '每天'
        else:
            prefix = '每周' + '、'.join(CronParser.WEEKDAYS[d] for d in sorted(dows))

        if hour == '*' and minute.startswith('*/'):
            return f"每 {minute[2:]} 分钟" if dow == '*' else f"{prefix} 每 {minute[2:]} 分钟"
        if len(minutes) == 1:
            m = minutes[0]
            if hour == '*':
                return '每小时' if dow == '*' else f"{prefix} 每小时"
            if hour.startswith('*/'):
                return f"每 {hour[2:]} 小时" if dow == '*' else f"{prefix} 每 {hour[2:]} 小时"
            if len(hours) <= 6:
                times = '、'.join(f"{h:02d}:{m:02d}" for h in hours)
                return f"{prefix} {times}"
        return f"自定义周期 ({minute} {hour} {dom} {month} {dow})"


class DingTalkRobot:
    """钉钉机器人类"""

    # 发送过快（每分钟最多 20 条）等可重试的钉钉错误码
    RETRYABLE_ERRCODES = {130101, -1}

    def __init__(self, webhook: str, secret: Optional[str] = None, dry_run: bool = False):
        self.webhook = webhook
        self.secret = secret
        self.dry_run = dry_run

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
        if at_all and '@所有人' not in text:
            text += "\n\n@所有人"
        if len(text.encode('utf-8')) > DINGTALK_MAX_BYTES:
            text = text.encode('utf-8')[:DINGTALK_MAX_BYTES].decode('utf-8', 'ignore')
            text += "\n\n> ⚠️ 消息过长已截断，完整数据请查看 monitor.log"
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

    def send_alert_card(self, critical_vps: List[Dict], critical_threshold: float = 90) -> Dict:
        """发送独立紧急告警，确保关键信息不被长报告淹没"""
        if len(critical_vps) == 1:
            v = critical_vps[0]
            title = f"🚨 {v['name']} 流量已用 {fmt_pct(v['bandwidth']['usage_percent'])}"
        else:
            title = f"🚨 {len(critical_vps)} 台 VPS 流量告急"

        text = "## 🚨 流量紧急告警\n\n"
        text += f"以下 {len(critical_vps)} 台 VPS 流量已超过 **{critical_threshold}%**：\n\n"
        for v in critical_vps:
            bw = v['bandwidth']
            text += f"**{v['name']}** 已用 **{fmt_pct(bw['usage_percent'])}**"
            text += f"（{bw['used_gb']:.1f} / {bw['total_gb']:.1f} GB）\n\n"
            text += f"- {VPSMonitor.describe_remaining(bw)}\n"
            if bw['reset_valid']:
                text += f"- 重置时间：{bw['reset_datetime']}（{bw['reset_in']}后）\n"
            text += "\n"
        text += "> 建议：暂停或限速高流量服务；如需继续使用，请在 KiwiVM 升级套餐。超额后 VPS 可能被暂停或限速。"
        return self.send_markdown(title=title, text=text, at_all=True)

    def _send_request(self, data: Dict, retries: int = 3) -> Dict:
        """发送请求到钉钉，网络异常或限流时自动重试"""
        if self.dry_run:
            print(f"\n===== [dry-run] {data['markdown']['title']} (@所有人: {data['at']['isAtAll']}) =====")
            print(data['markdown']['text'])
            print("=" * 60)
            return {'errcode': 0, 'errmsg': 'dry-run'}

        result = {'errcode': -1, 'errmsg': 'not sent'}
        for attempt in range(retries):
            try:
                response = requests.post(
                    self.webhook,
                    json=data,
                    params=self._generate_sign(),
                    timeout=10
                )
                try:
                    result = response.json()
                except ValueError:
                    result = {'errcode': -1, 'errmsg': f'HTTP {response.status_code}，响应非 JSON'}

                if result.get('errcode') == 0:
                    logger.info(f"✅ 钉钉消息发送成功: {data['markdown']['title']}")
                    return result
                if result.get('errcode') not in self.RETRYABLE_ERRCODES:
                    logger.error(f"❌ 钉钉消息发送失败 ({result.get('errcode')}): {result.get('errmsg')}")
                    return result
                err = result.get('errmsg')
            except (requests.ConnectionError, requests.Timeout) as e:
                err = redact(e)
                result = {'errcode': -1, 'errmsg': err}
            except Exception as e:
                logger.error(f"❌ 钉钉请求异常: {redact(e)}")
                return {'errcode': -1, 'errmsg': redact(e)}

            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning(f"钉钉发送失败，{wait}s 后重试: {err}")
                time.sleep(wait)
            else:
                logger.error(f"❌ 钉钉发送最终失败: {err}")
        return result


class BandwagonAPI:
    """搬瓦工 API 封装"""

    BASE_URL = "https://api.64clouds.com/v1"

    def __init__(self, veid: str, api_key: str):
        self.veid = veid
        self.api_key = api_key
        self.last_error: Optional[str] = None

    def _request(self, endpoint: str, params: Dict = None, retries: int = 3, timeout: int = 15) -> Optional[Dict]:
        """通用请求方法，网络异常或服务端错误自动重试；失败原因记录在 last_error"""
        url = f"{self.BASE_URL}/{endpoint}"

        request_params = {
            'veid': self.veid,
            'api_key': self.api_key
        }

        if params:
            request_params.update(params)

        for attempt in range(retries):
            try:
                response = requests.get(url, params=request_params, timeout=timeout)
                if response.status_code >= 500:
                    raise requests.ConnectionError(f"HTTP {response.status_code}")
                data = response.json()

                if data.get('error') == 0:
                    return data
                # 业务错误（如密钥错误）重试无意义，直接返回
                self.last_error = data.get('message') or f"错误码 {data.get('error')}"
                logger.error(f"API 错误 ({self.veid}/{endpoint}): {self.last_error}")
                return None
            except (requests.ConnectionError, requests.Timeout, ValueError) as e:
                self.last_error = '网络超时' if isinstance(e, requests.Timeout) else (
                    '响应格式异常' if isinstance(e, ValueError) else '网络连接失败')
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"API 请求失败 ({self.veid}/{endpoint})，{wait}s 后重试: {redact(e)}")
                    time.sleep(wait)
                else:
                    logger.error(f"API 请求最终失败 ({self.veid}/{endpoint}): {redact(e)}")
            except Exception as e:
                self.last_error = '请求异常'
                logger.error(f"API 请求异常 ({self.veid}/{endpoint}): {redact(e)}")
                return None
        return None

    def get_service_info(self) -> Optional[Dict]:
        """获取服务信息"""
        return self._request('getServiceInfo')

    def get_live_info(self) -> Optional[Dict]:
        """获取实时运行状态（ve_status 等）；该接口较慢，失败不重试太多次"""
        return self._request('getLiveServiceInfo', retries=2, timeout=20)


class VPSMonitor:
    """VPS 监控核心类"""

    def __init__(self, robot: DingTalkRobot):
        self.robot = robot

    @staticmethod
    def _shift_month(dt: datetime, months: int) -> datetime:
        """按自然月平移日期，日期超出目标月天数时取月末"""
        month_index = dt.month - 1 + months
        year = dt.year + month_index // 12
        month = month_index % 12 + 1
        day = min(dt.day, calendar.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)

    @staticmethod
    def calculate_bandwidth(data: Dict, now: datetime = None) -> Dict:
        """计算流量信息"""
        multiplier = data.get('monthly_data_multiplier') or 1
        total_bytes = data.get('plan_monthly_data') or 0
        used_bytes = data.get('data_counter') or 0

        # plan_monthly_data 是配额上限，不乘倍率；data_counter 是原始传输量，需乘倍率得到计费量
        total_gb = total_bytes / (1024**3)
        used_gb = used_bytes * multiplier / (1024**3)
        remaining_gb = max(0, total_gb - used_gb)
        usage_percent = (used_gb / total_gb * 100) if total_gb > 0 else 0

        # 计算重置时间
        reset_timestamp = data.get('data_next_reset') or 0
        reset_valid = reset_timestamp > 0
        now = now or datetime.now()

        if reset_valid:
            reset_date = datetime.fromtimestamp(reset_timestamp)
            seconds_until_reset = max(0.0, (reset_date - now).total_seconds())
            # 搬瓦工按自然月重置，反推本周期起始日（API 无 cycle_start 字段）
            cycle_start = VPSMonitor._shift_month(reset_date, -1)
            cycle_days = max(1.0, (reset_date - cycle_start).total_seconds() / 86400)
            # 用带小数的天数，避免周期初期因整天截断导致日均被放大
            days_passed = min(cycle_days, max(1.0, (now - cycle_start).total_seconds() / 86400))
        else:
            reset_date = None
            seconds_until_reset = 0.0
            cycle_days = 30.0
            days_passed = 1.0

        days_until_reset_f = seconds_until_reset / 86400

        # 日均使用量与超标预测
        daily_avg = used_gb / days_passed
        if reset_valid and days_until_reset_f > 0:
            predicted_usage = used_gb + daily_avg * days_until_reset_f
            will_exceed = predicted_usage > total_gb
        else:
            predicted_usage = used_gb
            will_exceed = False

        # 消耗速率分级: 实际日均 vs 理论均匀日均
        theoretical_daily = total_gb / cycle_days if total_gb > 0 else 0
        burn_rate = daily_avg / theoretical_daily if theoretical_daily > 0 else 0

        # 剩余可用天数估算（无消耗时为 None）
        estimated_days_left = round(remaining_gb / daily_avg, 1) if daily_avg > 0 else None

        return {
            'total_gb': round(total_gb, 2),
            'used_gb': round(used_gb, 2),
            'remaining_gb': round(remaining_gb, 2),
            'usage_percent': round(usage_percent, 2),
            'reset_date': reset_date.strftime('%Y-%m-%d') if reset_date else '未知',
            'reset_datetime': reset_date.strftime('%m-%d %H:%M') if reset_date else '未知',
            'days_until_reset': int(days_until_reset_f),
            'reset_in': fmt_duration(seconds_until_reset),
            'reset_valid': reset_valid,
            'daily_avg': round(daily_avg, 2),
            'predicted_usage': round(predicted_usage, 2),
            'will_exceed': will_exceed,
            'burn_rate': round(burn_rate, 2),
            'estimated_days_left': estimated_days_left
        }

    @staticmethod
    def describe_remaining(bw: Dict) -> str:
        """剩余流量 + 可撑多久的一句话描述"""
        text = f"剩余 **{fmt_gb(bw['remaining_gb'])}**"
        if bw['remaining_gb'] <= 0:
            return text + "，**已用尽**"
        days_left = bw.get('estimated_days_left')
        if days_left is None:
            return text
        if bw['reset_valid'] and not bw['will_exceed']:
            return text + "，按当前速度可撑到重置"
        if days_left < 1:
            return text + "，按当前速度 **不到 1 天** 用完"
        return text + f"，按当前速度约 **{int(days_left)} 天** 用完"

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
        """生成进度条（超过 100% 时进度条封顶）"""
        ratio = min(max(percent, 0), 100) / 100 * length
        filled = int(ratio)
        bar = '█' * filled
        if ratio - filled >= 0.5 and filled < length:
            bar += '▓'
            filled += 1
        bar += '░' * (length - filled)
        return f"{bar} {fmt_pct(percent)}"

    @staticmethod
    def format_bytes(bytes_value: int) -> str:
        """格式化字节数"""
        bytes_value = float(bytes_value or 0)
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
            'starting': '🔄 启动中',
            'stopped': '🔴 已停止',
            'suspended': '⛔ 已暂停',
        }
        return status_map.get((ve_status or '').lower(), '⚪ 状态未知')

    @staticmethod
    def is_abnormal_state(vps: Dict) -> bool:
        """VPS 处于暂停或停止状态"""
        return bool(vps.get('suspended')) or (vps.get('ve_status') or '').lower() == 'stopped'

    @staticmethod
    def get_trend_arrow(current_gb: float, last_gb: Optional[float]) -> str:
        """根据与上次检查的差值生成趋势描述"""
        if last_gb is None:
            return ''
        diff = current_gb - last_gb
        if diff < -0.1:
            # 计费量在周期内只增不减，下降说明已进入新周期
            return '🔄 已重置'
        if diff > 0.5:
            return f'↑ +{diff:.1f} GB'
        if diff > 0.1:
            return f'↗ +{diff:.1f} GB'
        return '→ 持平'

    @staticmethod
    def get_burn_rate_label(burn_rate: float) -> str:
        """消耗速率分级标签"""
        if burn_rate > 1.5:
            return '⚡消耗偏快'
        elif burn_rate < 0.5:
            return '🐢消耗很低'
        return ''

    def check_single_vps(self, veid: str, api_key: str, name: str = None) -> Tuple[Optional[Dict], Optional[str]]:
        """检查单个 VPS，合并服务信息和实时状态，返回 (结果, 失败原因)"""
        api = BandwagonAPI(veid, api_key)
        data = api.get_service_info()

        if not data:
            return None, api.last_error or '未知错误'

        bandwidth = self.calculate_bandwidth(data)

        # 获取实时运行状态（失败不影响主流程）
        live_data = api.get_live_info()
        ve_status = 'unknown'
        if live_data:
            ve_status = (live_data.get('ve_status') or 'unknown').lower()

        return {
            'veid': str(veid),
            'name': name or data.get('hostname') or str(veid),
            'hostname': data.get('hostname', 'N/A'),
            'location': data.get('node_location', 'N/A'),
            'plan': data.get('plan', 'N/A'),
            'plan_disk': self.format_bytes(data.get('plan_disk', 0)),
            'plan_ram': self.format_bytes(data.get('plan_ram', 0)),
            'ip_addresses': data.get('ip_addresses', []),
            'bandwidth': bandwidth,
            'os': data.get('os', 'N/A'),
            'suspended': bool(data.get('suspended', False)),
            've_status': ve_status
        }, None

    def check_vps_list(self, vps_configs: List[Dict]) -> Tuple[List[Dict], List[Tuple[str, str]]]:
        """并发批量检查 VPS 列表，返回 (成功结果, [(失败名称, 原因)])，均按配置顺序排列"""
        results = {}
        failed = {}

        logger.info(f"开始检查 {len(vps_configs)} 台 VPS...")

        with ThreadPoolExecutor(max_workers=min(len(vps_configs), 5)) as executor:
            futures = {
                executor.submit(
                    self.check_single_vps,
                    c['veid'], c['api_key'], c.get('name')
                ): (i, c)
                for i, c in enumerate(vps_configs)
            }
            for future in as_completed(futures):
                i, config = futures[future]
                name = config.get('name') or str(config['veid'])
                try:
                    result, error = future.result()
                    if result:
                        results[i] = result
                        logger.info(f"  ✅ {name} - 使用率: {result['bandwidth']['usage_percent']}%")
                    else:
                        failed[i] = (name, error)
                        logger.warning(f"  ❌ {name} - 查询失败: {error}")
                except Exception as e:
                    failed[i] = (name, '程序异常')
                    logger.error(f"  ❌ {name} - 异常: {redact(e)}")

        logger.info(f"检查完成: 成功 {len(results)}/{len(vps_configs)}")
        return [results[i] for i in sorted(results)], [failed[i] for i in sorted(failed)]

    @staticmethod
    def _headline(vps: Dict, others: int) -> str:
        """标题中的主角：名称 + 使用率（+ 等 N 台）"""
        text = f"{vps['name']} {fmt_pct(vps['bandwidth']['usage_percent'])}"
        if others > 0:
            text += f" 等 {others + 1} 台"
        return text

    def build_report(self, vps_list: List[Dict], critical_threshold: float = 90, alert_threshold: float = 80,
                     warning_threshold: float = 60, failed: List[Tuple[str, str]] = None,
                     last_history: Optional[Dict] = None,
                     next_run: Tuple[Optional[datetime], str] = (None, '')) -> Tuple[str, str]:
        """组装报告，返回 (通知标题, Markdown 正文)"""
        failed = failed or []
        now = datetime.now()
        next_run_time, next_run_desc = next_run

        # 历史数据用于趋势对比，优先按 veid 匹配，兼容只记录了 name 的旧数据
        last_map = {}
        if last_history:
            for v in last_history.get('vps', []):
                last_map[v.get('veid') or v.get('name')] = v.get('used_gb')

        def last_used(vps: Dict) -> Optional[float]:
            return last_map.get(vps['veid'], last_map.get(vps['name']))

        # ==================== 分级统计（按使用率降序） ====================
        sorted_vps = sorted(vps_list, key=lambda x: x['bandwidth']['usage_percent'], reverse=True)
        total_count = len(sorted_vps)
        critical = [v for v in sorted_vps if v['bandwidth']['usage_percent'] >= critical_threshold]
        alert = [v for v in sorted_vps if alert_threshold <= v['bandwidth']['usage_percent'] < critical_threshold]
        warning = [v for v in sorted_vps if warning_threshold <= v['bandwidth']['usage_percent'] < alert_threshold]
        normal = [v for v in sorted_vps if v['bandwidth']['usage_percent'] < warning_threshold]
        abnormal_state = [v for v in sorted_vps if self.is_abnormal_state(v)]

        total_bandwidth = sum(v['bandwidth']['total_gb'] for v in sorted_vps)
        total_used = sum(v['bandwidth']['used_gb'] for v in sorted_vps)
        overall_usage = (total_used / total_bandwidth * 100) if total_bandwidth > 0 else 0
        will_exceed = [v for v in sorted_vps if v['bandwidth']['will_exceed']]

        # ==================== 标题（即通知预览，直接点名最严重的 VPS） ====================
        if critical:
            title = f"🔴 流量严重预警：{self._headline(critical[0], len(critical) - 1)}"
            header = f"🔴 流量严重预警 · {len(critical)} 台超过 {critical_threshold}%"
        elif alert:
            title = f"🟠 流量预警：{self._headline(alert[0], len(alert) - 1)}"
            header = f"🟠 流量预警 · {len(alert)} 台超过 {alert_threshold}%"
        elif warning:
            title = f"🟡 流量提醒：{self._headline(warning[0], len(warning) - 1)}"
            header = f"🟡 流量提醒 · {len(warning)} 台超过 {warning_threshold}%"
        elif abnormal_state:
            title = f"⛔ VPS 状态异常：{abnormal_state[0]['name']}"
            header = f"⛔ {len(abnormal_state)} 台 VPS 未在运行"
        elif failed:
            title = f"⚠️ VPS 流量日报：{len(failed)} 台查询失败"
            header = f"⚠️ VPS 流量日报 · {len(failed)} 台查询失败"
        else:
            title = f"✅ VPS 流量正常 · {total_count} 台"
            header = "✅ VPS 流量正常"

        text = f"## {header}\n\n"
        text += f"**{now.strftime('%m-%d %H:%M')}** · 共 {total_count + len(failed)} 台 · "
        text += f"已用 {total_used:.1f} / {total_bandwidth:.1f} GB（{fmt_pct(overall_usage)}）\n\n"

        dist_parts = []
        for icon, group in (('🔴', critical), ('🟠', alert), ('🟡', warning), ('🟢', normal)):
            if group:
                dist_parts.append(f"{icon} {len(group)}")
        if failed:
            dist_parts.append(f"❌ {len(failed)}")
        text += f"{' · '.join(dist_parts)}\n\n"

        if will_exceed:
            names = '、'.join(v['name'] for v in will_exceed)
            text += f"> ⚠️ 按当前速度，**{names}** 将在重置前超出配额\n\n"

        text += "---\n\n"

        # ==================== 需关注：≥warning 或状态异常，展开详情 ====================
        detail_vps = [v for v in sorted_vps
                      if v['bandwidth']['usage_percent'] >= warning_threshold or self.is_abnormal_state(v)]
        summary_vps = [v for v in sorted_vps if v not in detail_vps]

        for vps in detail_vps:
            bw = vps['bandwidth']
            icon = self.get_status_icon(bw['usage_percent'], critical_threshold, alert_threshold, warning_threshold)
            trend = self.get_trend_arrow(bw['used_gb'], last_used(vps))
            burn_label = self.get_burn_rate_label(bw['burn_rate'])

            text += f"#### {icon} {vps['name']} · {fmt_pct(bw['usage_percent'])}\n\n"
            text += f"{self.get_progress_bar(bw['usage_percent'])}\n\n"
            if self.is_abnormal_state(vps) or vps.get('ve_status') not in ('running', 'unknown'):
                text += f"- 状态：**{self.get_ve_status_icon(vps.get('ve_status'), vps['suspended'])}**\n"
            used_line = f"- 已用 **{bw['used_gb']:.1f} / {bw['total_gb']:.1f} GB**"
            if trend:
                used_line += f"，较上次 {trend}"
            text += used_line + "\n"
            text += f"- {self.describe_remaining(bw)}\n"
            if bw['reset_valid']:
                text += f"- 重置：{bw['reset_datetime']}（{bw['reset_in']}后）"
                if bw['will_exceed']:
                    text += f"，届时预计用到 {bw['predicted_usage']:.0f} GB，**会超额**"
                text += "\n"
            else:
                text += "- 重置：时间未知\n"
            if bw['daily_avg'] > 0 and bw['reset_valid']:
                text += f"- 日均 {bw['daily_avg']:.1f} GB"
                if burn_label:
                    text += f" {burn_label}"
                text += "\n"
            text += f"- {vps['location']} · {vps['plan']}\n"

            if bw['usage_percent'] >= critical_threshold:
                text += "- 💡 **流量即将耗尽**，请立即限速/暂停高流量服务或升级套餐\n"
            elif bw['usage_percent'] >= alert_threshold:
                text += "- 💡 建议排查流量来源，必要时限速或升级套餐\n"
            elif bw['will_exceed']:
                text += "- 💡 消耗偏快，建议留意流量来源\n"

            text += "\n---\n\n"

        # ==================== 正常：折叠为一行 ====================
        if summary_vps:
            text += f"#### 🟢 正常 {len(summary_vps)} 台\n\n"
            for vps in summary_vps:
                bw = vps['bandwidth']
                line = f"- **{vps['name']}** {fmt_pct(bw['usage_percent'])}（{bw['used_gb']:.1f} / {bw['total_gb']:.0f} GB）"
                trend = self.get_trend_arrow(bw['used_gb'], last_used(vps))
                if trend:
                    line += f" {trend}"
                if bw['reset_valid']:
                    line += f" · {bw['reset_in']}后重置"
                if vps.get('ve_status') not in ('running', 'unknown'):
                    line += f" · {self.get_ve_status_icon(vps.get('ve_status'), vps['suspended'])}"
                text += line + "\n"
            text += "\n---\n\n"

        # ==================== 查询失败 ====================
        if failed:
            text += f"#### ❌ 查询失败 {len(failed)} 台\n\n"
            for name, reason in failed:
                text += f"- **{name}**：{reason}\n"
            text += "\n> 请检查 veid / API 密钥是否正确，或稍后重试\n\n---\n\n"

        # ==================== 页脚：阈值 + 下次检查 ====================
        footer = f"阈值 🟡{warning_threshold}% 🟠{alert_threshold}% 🔴{critical_threshold}%"
        if next_run_time:
            footer += f" · 下次检查 {next_run_time.strftime('%H:%M')}（{fmt_duration((next_run_time - now).total_seconds())}后，{next_run_desc}）"
        text += f"> {footer}\n"

        return title, text

    def send_detailed_report(self, vps_list: List[Dict], critical_threshold: float = 90, alert_threshold: float = 80,
                             warning_threshold: float = 60, failed: List[Tuple[str, str]] = None) -> bool:
        """发送详细报告（分层展示 + 历史对比），🔴 级别额外发送独立告警；返回是否全部发送成功"""
        title, text = self.build_report(
            vps_list, critical_threshold, alert_threshold, warning_threshold,
            failed=failed, last_history=load_last_history(), next_run=CronParser.get_next_run_time()
        )

        critical = sorted([v for v in vps_list if v['bandwidth']['usage_percent'] >= critical_threshold],
                          key=lambda x: x['bandwidth']['usage_percent'], reverse=True)
        # 有 🔴 时由独立告警负责 @所有人，避免同一次检查连续 @ 两次
        at_all = not critical and any(v['bandwidth']['usage_percent'] >= alert_threshold for v in vps_list)
        ok = self.robot.send_markdown(title=title, text=text, at_all=at_all).get('errcode') == 0

        if critical:
            ok = self.robot.send_alert_card(critical, critical_threshold).get('errcode') == 0 and ok
        return ok

    def monitor_and_report(self, vps_configs: List[Dict], critical_threshold: float = 90, alert_threshold: float = 80,
                           warning_threshold: float = 60, save: bool = True) -> bool:
        """监控并发送详细报告，返回本次执行是否成功"""
        results, failed = self.check_vps_list(vps_configs)

        if not results:
            logger.error(f"❌ 所有 VPS 查询失败: {failed}")
            text = "## ❌ VPS 监控执行失败\n\n"
            text += f"本次 {len(failed)} 台 VPS 全部查询失败，未能生成流量报告。\n\n"
            for name, reason in failed:
                text += f"- **{name}**：{reason}\n"
            text += "\n> 请检查服务器网络能否访问 api.64clouds.com，以及 veid / API 密钥是否正确\n\n"
            text += f"> {datetime.now().strftime('%m-%d %H:%M')}\n"
            self.robot.send_markdown(title='❌ VPS 监控执行失败', text=text, at_all=True)
            return False

        sent = self.send_detailed_report(results, critical_threshold, alert_threshold, warning_threshold, failed=failed)
        if save:
            save_history(results)

        alerts = [v for v in results if v['bandwidth']['usage_percent'] >= alert_threshold]
        if alerts:
            logger.warning(f"⚠️  发现 {len(alerts)} 台 VPS 超过阈值 {alert_threshold}%")
        else:
            logger.info(f"✅ 所有 VPS 流量正常（低于 {alert_threshold}%）")
        return sent


def load_config(config_file: str = None) -> Dict:
    """加载并校验配置文件，敏感字段支持环境变量覆盖"""
    if config_file is None:
        config_file = str(SCRIPT_DIR / 'config.json')

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"✅ 配置文件加载成功: {config_file}")
    except FileNotFoundError:
        logger.error(f"❌ 配置文件不存在: {config_file}（可参考 config.example.json 创建）")
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error(f"❌ 配置文件格式错误: {e}")
        sys.exit(1)

    if not isinstance(config, dict):
        logger.error("❌ 配置校验失败: 配置文件顶层必须是对象")
        sys.exit(1)

    # 环境变量覆盖钉钉凭据（先于校验，允许 config.json 中不写 webhook/secret）
    dingtalk = config.get('dingtalk')
    if dingtalk is None and os.environ.get('DINGTALK_WEBHOOK'):
        dingtalk = config['dingtalk'] = {}
    if isinstance(dingtalk, dict):
        if os.environ.get('DINGTALK_WEBHOOK'):
            dingtalk['webhook'] = os.environ['DINGTALK_WEBHOOK']
        if os.environ.get('DINGTALK_SECRET'):
            dingtalk['secret'] = os.environ['DINGTALK_SECRET']

    # 配置 schema 校验
    errors = []
    if not isinstance(dingtalk, dict):
        errors.append("缺少 dingtalk 配置段")
    elif not isinstance(dingtalk.get('webhook'), str) or not dingtalk['webhook'].startswith('http'):
        errors.append("缺少 dingtalk.webhook 或格式不正确（应以 http 开头）")
    if 'vps_list' not in config or not isinstance(config.get('vps_list'), list):
        errors.append("缺少 vps_list 或格式不是数组")
    elif len(config['vps_list']) == 0:
        errors.append("vps_list 不能为空")
    else:
        seen = set()
        for i, vps in enumerate(config['vps_list']):
            if not isinstance(vps, dict):
                errors.append(f"vps_list[{i}] 必须是对象，实际是 {type(vps).__name__}")
                continue
            if not vps.get('veid') or 'api_key' not in vps:
                errors.append(f"vps_list[{i}] 缺少 veid 或 api_key")
                continue
            veid = str(vps['veid'])
            if veid in seen:
                errors.append(f"vps_list[{i}] 的 veid {veid} 重复")
            seen.add(veid)
            # 环境变量覆盖 API 密钥
            vps['api_key'] = os.environ.get(f"VPS_{veid}_API_KEY", vps['api_key'])
            if not vps['api_key']:
                errors.append(f"vps_list[{i}] ({veid}) 的 api_key 为空")
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
    except Exception as e:
        logger.warning(f"历史记录读取失败，本次不做趋势对比: {e}")
        return None


def save_history(results: List[Dict]):
    """将执行摘要追加写入 history.jsonl，用于趋势分析"""
    record = {
        'timestamp': datetime.now().isoformat(),
        'vps': [{
            'veid': r['veid'],
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
    parser.add_argument('--dry-run', action='store_true',
                        help='只在终端打印消息内容，不发送钉钉、不写历史记录')
    args = parser.parse_args()

    setup_logging()

    logger.info("=" * 50)
    logger.info("🚀 VPS 流量监控系统启动" + ("（dry-run）" if args.dry_run else ""))
    logger.info("=" * 50)

    # 加载配置
    config = load_config(args.config)

    # 初始化钉钉机器人
    robot = DingTalkRobot(
        webhook=config['dingtalk']['webhook'],
        secret=config['dingtalk'].get('secret'),
        dry_run=args.dry_run
    )

    # 初始化监控器
    monitor = VPSMonitor(robot)

    # 执行监控并发送详细报告
    ok = monitor.monitor_and_report(
        vps_configs=config['vps_list'],
        critical_threshold=config['monitor'].get('critical_threshold', 90),
        alert_threshold=config['monitor']['alert_threshold'],
        warning_threshold=config['monitor'].get('warning_threshold', 60),
        save=not args.dry_run
    )

    logger.info("=" * 50)
    logger.info("✅ 监控任务完成" if ok else "⚠️ 监控任务完成，但存在失败（详见上方日志）")
    logger.info("=" * 50)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
