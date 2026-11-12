"""Private, confirmed subscription drafts; native scheduling remains the Agent's responsibility."""
from datetime import time
import json
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from archive import ArchiveError, require, write_atomic
from market_scope import normalize_markets


def configure(path, *, owner, markets, destination, confirmed=False, frequency='weekly',
              weekdays=('MO',), local_time='11:30', timezone='Asia/Shanghai'):
    require(confirmed is True, '订阅 / 等待用户确认国家、时间和接收位置，不自动创建')
    require(isinstance(owner,str) and owner.strip() and isinstance(destination,str) and destination.strip(),
            '订阅 / 必须确认运营归属和接收位置')
    markets = normalize_markets(markets)
    require(frequency in ('weekly','daily'), '订阅 / 当前配置支持每日或每周；其他频率由原生调度器验证后适配')
    days = ('MO','TU','WE','TH','FR','SA','SU')
    require(weekdays and set(weekdays) <= set(days), '订阅 / 每周日期无效')
    weekdays = [day for day in days if frequency=='daily' or day in weekdays]
    try:
        require(len(local_time)==5 and time.fromisoformat(local_time).strftime('%H:%M')==local_time,
                '订阅 / 时间须为24小时HH:MM')
        ZoneInfo(timezone)
    except (ValueError,TypeError,ZoneInfoNotFoundError):
        raise ArchiveError('订阅 / 时间或时区无效，不修改原计划') from None
    old = {}
    if path.exists():
        try: old = json.loads(path.read_text())
        except (ValueError,OSError):
            raise ArchiveError('订阅 / 旧配置无法读取，请管理员核对，不覆盖原任务') from None
        require(isinstance(old,dict) and old.get('owner')==owner and old.get('subscription_id'),
                '订阅 / 旧配置归属不明，不接管他人任务')
    result = {'subscription_id':old.get('subscription_id') or str(uuid4()), 'owner':owner,
              'markets':markets,'destination':destination,'frequency':frequency,'weekdays':weekdays,
              'local_time':local_time,'timezone':timezone,'window':'previous_7_complete_days',
              'schedule_confirmed':True,'status':'waiting_for_validation',
              'scheduler_job_id':old.get('scheduler_job_id')}
    if old and all(old.get(k)==v for k,v in result.items() if k!='status'):
        result['status'] = old.get('status','waiting_for_validation')
    # Only user routing and schedule belong here. Credentials are stored by the host's secret manager.
    write_atomic(path,json.dumps(result,ensure_ascii=False).encode())
    path.chmod(0o600)
    return result
