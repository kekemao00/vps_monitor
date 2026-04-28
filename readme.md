```bash
# 1. 普通用户设置 crontab
crontab -e

# 2. 添加任务
0 9,21 * * * /usr/bin/python3 /opt/vps_monitor/vps_monitor.py >> /opt/vps_monitor/cron.log 2>&1

# 3. 普通用户测试
python3 /opt/vps_monitor/vps_monitor.py

```



```bash
# 1. 确认当前用户
whoami
# 应该显示: xingkeqi

# 2. 查看当前用户的 crontab
crontab -l

# 3. 如果没有，设置 crontab
crontab -e
# 添加: 0 9,21 * * * /usr/bin/python3 /opt/vps_monitor/vps_monitor.py >> /opt/vps_monitor/cron.log 2>&1

# 4. 验证设置
crontab -l

# 5. 用普通用户测试（不要 sudo）
python3 /opt/vps_monitor/vps_monitor.py

# 6. 查看日志
tail -n 30 /opt/vps_monitor/monitor.log

# 7. 检查钉钉消息底部是否显示正确的时间

```



```bash
# 修改日志文件的所有者为当前用户
sudo chown xingkeqi:xingkeqi /opt/vps_monitor/monitor.log
sudo chown xingkeqi:xingkeqi /opt/vps_monitor/cron.log

# 修改整个目录的所有者（推荐）
sudo chown -R xingkeqi:xingkeqi /opt/vps_monitor/

# 设置合适的权限
chmod 755 /opt/vps_monitor/
chmod 644 /opt/vps_monitor/*.log
chmod 644 /opt/vps_monitor/*.json
chmod 755 /opt/vps_monitor/*.py

```



```bash
# 查看 cron 服务状态
sudo systemctl status cron

# 如果未运行，启动它
sudo systemctl start cron
sudo systemctl enable cron

```

