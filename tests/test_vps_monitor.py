import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vps_monitor as vm  # noqa: E402

GB = 1024 ** 3


def service_info(used_gb, total_gb=1000, reset=datetime(2026, 10, 12, 18, 0), multiplier=1):
    return {
        'error': 0,
        'hostname': 'host',
        'node_location': 'US, Los Angeles',
        'plan': 'kvmv5-1000',
        'plan_monthly_data': total_gb * GB,
        'data_counter': used_gb * GB / multiplier,
        'monthly_data_multiplier': multiplier,
        'data_next_reset': reset.timestamp(),
        'suspended': False,
    }


def make_vps(veid, name, used_gb, now, total_gb=1000, ve_status='running'):
    bw = vm.VPSMonitor.calculate_bandwidth(service_info(used_gb, total_gb), now=now)
    return {'veid': veid, 'name': name, 'location': 'US, Los Angeles', 'plan': 'kvm',
            'bandwidth': bw, 'suspended': False, 've_status': ve_status}


class CronParserTest(unittest.TestCase):
    now = datetime(2026, 10, 3, 10, 10)

    def test_fixed_hours_with_minute(self):
        t, desc = vm.CronParser.parse('30', '9,21', now=self.now)
        self.assertEqual(t, datetime(2026, 10, 3, 21, 30))
        self.assertEqual(desc, '每天 09:30、21:30')

    def test_hourly_minute_later_this_hour(self):
        t, desc = vm.CronParser.parse('30', '*', now=self.now)
        self.assertEqual(t, datetime(2026, 10, 3, 10, 30))
        self.assertEqual(desc, '每小时')

    def test_every_n_hours(self):
        t, desc = vm.CronParser.parse('0', '*/6', now=self.now)
        self.assertEqual(t, datetime(2026, 10, 3, 12, 0))
        self.assertEqual(desc, '每 6 小时')

    def test_range_and_weekday(self):
        # 2026-10-03 是周六，下一个工作日是周一 10-05
        t, desc = vm.CronParser.parse('0', '9', '*', '*', '1-5', now=self.now)
        self.assertEqual(t, datetime(2026, 10, 5, 9, 0))
        self.assertEqual(desc, '每周一、二、三、四、五 09:00')

    def test_every_n_minutes(self):
        t, desc = vm.CronParser.parse('*/15', '*', now=self.now)
        self.assertEqual(t, datetime(2026, 10, 3, 10, 15))
        self.assertEqual(desc, '每 15 分钟')

    def test_invalid(self):
        t, _ = vm.CronParser.parse('x', '9', now=self.now)
        self.assertIsNone(t)


class BandwidthTest(unittest.TestCase):
    now = datetime(2026, 10, 3, 18, 0)  # 周期 09-12 18:00 → 10-12 18:00，已过 21 天

    def test_multiplier_and_prediction(self):
        bw = vm.VPSMonitor.calculate_bandwidth(service_info(840, multiplier=2), now=self.now)
        self.assertAlmostEqual(bw['used_gb'], 840)
        self.assertAlmostEqual(bw['daily_avg'], 40)
        self.assertTrue(bw['will_exceed'])
        self.assertEqual(bw['days_until_reset'], 9)
        self.assertAlmostEqual(bw['estimated_days_left'], 4.0)

    def test_low_usage_lasts_until_reset(self):
        bw = vm.VPSMonitor.calculate_bandwidth(service_info(100), now=self.now)
        self.assertFalse(bw['will_exceed'])
        self.assertIn('可撑到重置', vm.VPSMonitor.describe_remaining(bw))

    def test_no_reset_and_no_quota(self):
        data = service_info(0, total_gb=0)
        data['data_next_reset'] = 0
        bw = vm.VPSMonitor.calculate_bandwidth(data, now=self.now)
        self.assertFalse(bw['reset_valid'])
        self.assertIsNone(bw['estimated_days_left'])
        self.assertEqual(bw['usage_percent'], 0)

    def test_progress_bar_caps_over_100(self):
        bar = vm.VPSMonitor.get_progress_bar(130)
        self.assertTrue(bar.startswith('█' * 20 + ' '))

    def test_trend_detects_reset(self):
        self.assertEqual(vm.VPSMonitor.get_trend_arrow(5, 900), '🔄 已重置')
        self.assertEqual(vm.VPSMonitor.get_trend_arrow(10, 8), '↑ +2.0 GB')
        self.assertEqual(vm.VPSMonitor.get_trend_arrow(10, 10), '→ 持平')
        self.assertEqual(vm.VPSMonitor.get_trend_arrow(10, None), '')


class ReportTest(unittest.TestCase):
    now = datetime(2026, 10, 3, 18, 0)

    def build(self, vps, failed=None, history=None):
        monitor = vm.VPSMonitor(vm.DingTalkRobot('http://x', dry_run=True))
        return monitor.build_report(vps, 90, 80, 60, failed=failed, last_history=history)

    def test_critical_title_names_worst_vps(self):
        vps = [make_vps('1', 'a', 100, self.now), make_vps('2', 'b', 950, self.now), make_vps('3', 'c', 920, self.now)]
        title, text = self.build(vps)
        self.assertEqual(title, '🔴 流量严重预警：b 95.0% 等 2 台')
        # 详情按使用率降序
        self.assertLess(text.index('#### 🔴 b'), text.index('#### 🔴 c'))
        self.assertIn('#### 🟢 正常 1 台', text)

    def test_failed_reasons_listed(self):
        title, text = self.build([make_vps('1', 'a', 100, self.now)], failed=[('jp', 'Invalid API key')])
        self.assertIn('查询失败', title)
        self.assertIn('**jp**：Invalid API key', text)

    def test_history_matched_by_veid(self):
        history = {'vps': [{'veid': '1', 'name': 'old-name', 'used_gb': 99}]}
        _, text = self.build([make_vps('1', 'a', 100, self.now)], history=history)
        self.assertIn('↑ +1.0 GB', text)

    def test_stopped_vps_expanded(self):
        title, text = self.build([make_vps('1', 'a', 10, self.now, ve_status='stopped')])
        self.assertIn('状态异常', title)
        self.assertIn('🔴 已停止', text)

    def test_single_at_all_when_critical(self):
        robot = mock.Mock()
        robot.send_markdown.return_value = {'errcode': 0}
        robot.send_alert_card.return_value = {'errcode': 0}
        monitor = vm.VPSMonitor(robot)
        with mock.patch.object(vm, 'load_last_history', return_value=None), \
                mock.patch.object(vm.CronParser, 'get_next_run_time', return_value=(None, '')):
            monitor.send_detailed_report([make_vps('1', 'a', 950, self.now)], 90, 80, 60)
        self.assertFalse(robot.send_markdown.call_args.kwargs['at_all'])
        robot.send_alert_card.assert_called_once()


class RedactTest(unittest.TestCase):
    def test_redact(self):
        s = "Max retries exceeded with url: /v1/getServiceInfo?veid=1&api_key=private_abc (Caused by x)"
        self.assertNotIn('private_abc', vm.redact(s))
        self.assertEqual(vm.redact('/robot/send?access_token=tok&sign=abc'), '/robot/send?access_token=***&sign=***')


if __name__ == '__main__':
    unittest.main()
