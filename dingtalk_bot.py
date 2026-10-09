#!/usr/bin/env python3
"""
钉钉群 @机器人 指令服务

基于钉钉「企业内部应用机器人 + Stream 模式」：程序主动连到钉钉，不需要公网 IP 和回调地址。
群成员 @机器人 发送指令，即时查询 VPS 流量。定时报告仍由 vps_monitor.py + crontab 负责。
"""

import argparse
import asyncio
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import vps_monitor as vm

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "## 🤖 VPS 流量机器人\n\n"
    "@我 并发送以下指令：\n\n"
    "- **状态** — 查询全部 VPS 流量（同定时报告）\n"
    "- **查询 名称** — 查看单台 VPS 详情，名称也可以写 veid\n"
    "- **列表** — 列出已配置的 VPS\n"
    "- **帮助** — 显示本说明\n\n"
    "> 也支持英文：status / query 名称 / list / help"
)

COMMANDS = {
    'help': ('帮助', 'help', '?', '？', 'h'),
    'status': ('状态', '查询', '流量', '报告', 'status', 'report', 's'),
    'list': ('列表', 'list', 'ls'),
}


def parse_command(text: str) -> Tuple[str, str]:
    """把消息文本解析成 (指令, 参数)；无法识别时指令为 'unknown'"""
    parts = (text or '').strip().split(None, 1)
    if not parts:
        return 'help', ''
    word, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else '')
    for cmd, aliases in COMMANDS.items():
        if word in aliases:
            return cmd, arg
    return 'unknown', text.strip()


class CommandHandler:
    """指令处理：输入消息文本，输出 (标题, Markdown 正文)，不依赖钉钉 SDK，便于测试"""

    def __init__(self, config: Dict, monitor: vm.VPSMonitor = None):
        self.vps_configs = config['vps_list']
        m = config['monitor']
        self.critical = m.get('critical_threshold', 90)
        self.alert = m['alert_threshold']
        self.warning = m.get('warning_threshold', 60)
        self.monitor = monitor or vm.VPSMonitor(robot=None)

    def handle(self, text: str) -> Tuple[str, str]:
        cmd, arg = parse_command(text)
        if cmd == 'help':
            return '🤖 使用说明', HELP_TEXT
        if cmd == 'list':
            return self._list()
        if cmd == 'status':
            return self._query(arg) if arg else self._status()
        return '🤖 未识别的指令', f"没看懂「{arg[:50]}」\n\n{HELP_TEXT}"

    def _list(self) -> Tuple[str, str]:
        text = f"## 📋 已配置 {len(self.vps_configs)} 台 VPS\n\n"
        for c in self.vps_configs:
            text += f"- **{c.get('name') or c['veid']}**（veid {c['veid']}）\n"
        return '📋 VPS 列表', text

    def _status(self) -> Tuple[str, str]:
        results, failed = self.monitor.check_vps_list(self.vps_configs)
        if not results:
            text = "## ❌ 查询失败\n\n"
            for name, reason in failed:
                text += f"- **{name}**：{reason}\n"
            return '❌ VPS 查询失败', text
        # 只读查询：用上次记录做趋势对比，但不写历史，避免打乱定时报告的「较上次」
        return self.monitor.build_report(
            results, self.critical, self.alert, self.warning, failed=failed,
            last_history=vm.load_last_history(), next_run=vm.CronParser.get_next_run_time()
        )

    def find_vps(self, keyword: str) -> List[Dict]:
        """按 veid / 名称精确匹配，找不到时按名称包含匹配"""
        kw = keyword.strip().lower()
        exact = [c for c in self.vps_configs
                 if str(c['veid']).lower() == kw or (c.get('name') or '').lower() == kw]
        if exact:
            return exact
        return [c for c in self.vps_configs if kw in (c.get('name') or '').lower()]

    def _query(self, keyword: str) -> Tuple[str, str]:
        matches = self.find_vps(keyword)
        if not matches:
            _, listing = self._list()
            return '🤖 未找到 VPS', f"没有找到「{keyword[:50]}」\n\n{listing}"
        if len(matches) > 1:
            names = '、'.join(c.get('name') or str(c['veid']) for c in matches)
            return '🤖 匹配到多台', f"「{keyword[:50]}」匹配到多台：{names}\n\n请写完整名称或 veid"

        c = matches[0]
        name = c.get('name') or str(c['veid'])
        vps, error = self.monitor.check_single_vps(c['veid'], c['api_key'], c.get('name'))
        if not vps:
            return f'❌ {name} 查询失败', f"## ❌ {name} 查询失败\n\n{error}"

        last_gb = None
        last = vm.load_last_history()
        if last:
            for v in last.get('vps', []):
                if (v.get('veid') or v.get('name')) in (vps['veid'], vps['name']):
                    last_gb = v.get('used_gb')
        bw = vps['bandwidth']
        icon = vm.VPSMonitor.get_status_icon(bw['usage_percent'], self.critical, self.alert, self.warning)
        title = f"{icon} {vps['name']} 已用 {vm.fmt_pct(bw['usage_percent'])}"
        text = self.monitor.render_vps_detail(vps, self.critical, self.alert, self.warning, last_gb)
        return title, text


def run_stream(client_id: str, client_secret: str, handler: CommandHandler):
    """连接钉钉 Stream，收到 @消息 后在线程池里查询并回复"""
    try:
        import dingtalk_stream
    except ImportError:
        logger.error("❌ 缺少依赖 dingtalk-stream，请先执行: pip install -r requirements.txt")
        sys.exit(1)

    # 查询要调搬瓦工 API，耗时数秒；放到线程池里，先回 ACK，避免钉钉超时重推
    pool = ThreadPoolExecutor(max_workers=2)
    seen_ids: List[str] = []

    def reply(incoming) -> None:
        text = ''
        if incoming.message_type == 'text' and incoming.text:
            text = incoming.text.content
        logger.info(f"收到指令 [{incoming.conversation_title or '单聊'}] {incoming.sender_nick}: {text.strip()}")
        try:
            title, body = handler.handle(text)
        except Exception as e:
            logger.exception(f"指令处理异常: {vm.redact(e)}")
            title, body = '❌ 处理失败', '## ❌ 处理失败\n\n程序异常，详见 bot.log'
        # sessionWebhook 是本次会话的临时回复地址，无需加签
        vm.DingTalkRobot(incoming.session_webhook).send_markdown(title=title, text=body)

    class BotHandler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback):
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            # 网络抖动时钉钉可能重推同一条消息，按 msgId 去重
            if incoming.message_id in seen_ids:
                return dingtalk_stream.AckMessage.STATUS_OK, 'duplicate'
            seen_ids.append(incoming.message_id)
            del seen_ids[:-200]
            asyncio.get_running_loop().run_in_executor(pool, reply, incoming)
            return dingtalk_stream.AckMessage.STATUS_OK, 'OK'

    client = dingtalk_stream.DingTalkStreamClient(dingtalk_stream.Credential(client_id, client_secret))
    client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, BotHandler())
    logger.info("🤖 钉钉 Stream 已启动，等待 @机器人 指令...")
    client.start_forever()


def main():
    parser = argparse.ArgumentParser(description='钉钉群 @机器人 指令服务（Stream 模式）')
    parser.add_argument('--config', '-c', type=str, default=None,
                        help='配置文件路径（默认: 脚本目录下 config.json）')
    parser.add_argument('--cli', action='store_true',
                        help='不连钉钉，在终端输入指令测试回复内容')
    args = parser.parse_args()

    vm.setup_logging(vm.SCRIPT_DIR / 'bot.log')
    config = vm.load_config(args.config, require_webhook=False)
    handler = CommandHandler(config)

    if args.cli:
        print("输入指令测试（如: 帮助 / 状态 / 查询 名称），Ctrl+D 退出")
        for line in sys.stdin:
            title, body = handler.handle(line)
            print(f"\n===== {title} =====\n{body}\n")
        return

    bot = config.get('dingtalk_bot') or {}
    client_id = os.environ.get('DINGTALK_CLIENT_ID') or bot.get('client_id')
    client_secret = os.environ.get('DINGTALK_CLIENT_SECRET') or bot.get('client_secret')
    if not client_id or not client_secret:
        logger.error("❌ 缺少 dingtalk_bot.client_id / client_secret（钉钉开放平台 → 应用 → 凭证与基础信息）")
        sys.exit(1)
    run_stream(client_id, client_secret, handler)


if __name__ == '__main__':
    main()
