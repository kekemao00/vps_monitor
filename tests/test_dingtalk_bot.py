import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dingtalk_bot as bot  # noqa: E402
import vps_monitor as vm  # noqa: E402
from test_vps_monitor import make_vps  # noqa: E402

CONFIG = {
    'vps_list': [
        {'veid': '111', 'api_key': 'k1', 'name': 'la-cn2'},
        {'veid': '222', 'api_key': 'k2', 'name': 'la-dc9'},
        {'veid': '333', 'api_key': 'k3', 'name': 'tokyo'},
    ],
    'monitor': {'alert_threshold': 80},
}


class ParseCommandTest(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(bot.parse_command(' 状态 '), ('status', ''))
        self.assertEqual(bot.parse_command('STATUS'), ('status', ''))
        self.assertEqual(bot.parse_command('查询  tokyo '), ('status', 'tokyo'))
        self.assertEqual(bot.parse_command('列表'), ('list', ''))
        self.assertEqual(bot.parse_command(''), ('help', ''))

    def test_unknown(self):
        self.assertEqual(bot.parse_command('重启 tokyo'), ('unknown', '重启 tokyo'))


class CommandHandlerTest(unittest.TestCase):
    now = datetime(2026, 10, 3, 18, 0)

    def setUp(self):
        self.monitor = mock.Mock(wraps=vm.VPSMonitor(robot=None))
        self.handler = bot.CommandHandler(CONFIG, monitor=self.monitor)
        patches = [mock.patch.object(vm, 'load_last_history', return_value=None),
                   mock.patch.object(vm.CronParser, 'get_next_run_time', return_value=(None, ''))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_help_and_list_skip_api(self):
        self.assertIn('查询 名称', self.handler.handle('帮助')[1])
        self.assertIn('**tokyo**（veid 333）', self.handler.handle('list')[1])
        self.monitor.check_vps_list.assert_not_called()

    def test_status_builds_report_without_saving_history(self):
        vps = [make_vps('111', 'la-cn2', 950, self.now), make_vps('333', 'tokyo', 100, self.now)]
        self.monitor.check_vps_list.return_value = (vps, [])
        with mock.patch.object(vm, 'save_history') as save:
            title, _ = self.handler.handle('状态')
        self.assertTrue(title.startswith('🔴'))
        save.assert_not_called()

    def test_status_all_failed(self):
        self.monitor.check_vps_list.return_value = ([], [('tokyo', 'Invalid API key')])
        title, text = self.handler.handle('status')
        self.assertIn('失败', title)
        self.assertIn('Invalid API key', text)

    def test_query_by_name_and_veid(self):
        self.monitor.check_single_vps.return_value = (make_vps('333', 'tokyo', 700, self.now), None)
        title, text = self.handler.handle('查询 tokyo')
        self.assertEqual(title, '🟡 tokyo 已用 70.0%')
        self.assertIn('#### 🟡 tokyo', text)
        self.handler.handle('查询 333')
        self.assertEqual(self.monitor.check_single_vps.call_args.args[0], '333')

    def test_query_ambiguous_and_missing(self):
        self.assertIn('la-cn2、la-dc9', self.handler.handle('查询 la')[1])
        self.assertIn('没有找到', self.handler.handle('查询 paris')[1])
        self.monitor.check_single_vps.assert_not_called()

    def test_unknown_shows_help(self):
        title, text = self.handler.handle('重启 tokyo')
        self.assertIn('未识别', title)
        self.assertIn('帮助', text)


class LoadConfigTest(unittest.TestCase):
    def test_bot_config_without_webhook(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(CONFIG, f)
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict('os.environ', {}, clear=True):
            config = vm.load_config(f.name, require_webhook=False)
            self.assertEqual(len(config['vps_list']), 3)
            with self.assertRaises(SystemExit):
                vm.load_config(f.name)


if __name__ == '__main__':
    unittest.main()
