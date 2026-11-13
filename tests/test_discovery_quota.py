import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kadr_farm.discovery_quota import (
    DailyQuotaExceeded, QuotaLedgerError, YouTubeSearchQuotaLedger,
)

UTC = dt.timezone.utc


def test_pacific_day_key_tracks_daylight_saving_transition(tmp_path):
    ledger = YouTubeSearchQuotaLedger(tmp_path / 'quota.json')
    # 06:30 UTC is 22:30 PST on Nov 1; 08:30 UTC is 00:30 PST on Nov 2.
    before = ledger.status(now=dt.datetime(2026, 11, 2, 6, 30, tzinfo=UTC))
    after = ledger.status(now=dt.datetime(2026, 11, 2, 8, 30, tzinfo=UTC))
    assert before['pacific_date'] == '2026-11-01'
    assert after['pacific_date'] == '2026-11-02'


def test_external_calls_and_failed_attempts_reduce_remaining(tmp_path):
    ledger = YouTubeSearchQuotaLedger(tmp_path / 'quota.json', daily_ceiling=10)
    now = dt.datetime(2026, 11, 2, 12, tzinfo=UTC)
    ledger.set_external_usage(3, now=now)
    call_id = ledger.reserve_attempt(run_id='run-1', context={'order': 'date'}, now=now)
    ledger.finish_attempt(call_id, 'failed', error_reason='quotaExceeded', now=now)
    status = ledger.status(now=now)
    assert status['application_attempts'] == 1
    assert status['external_calls'] == 3
    assert status['remaining'] == 6
    payload = json.loads((tmp_path / 'quota.json').read_text(encoding='utf-8'))
    attempt = payload['days'][status['pacific_date']]['attempts'][0]
    assert attempt['outcome'] == 'failed'
    assert attempt['error_reason'] == 'quotaExceeded'


def test_ceiling_is_enforced_atomically_across_threads(tmp_path):
    ledger = YouTubeSearchQuotaLedger(tmp_path / 'quota.json', daily_ceiling=5)
    now = dt.datetime(2026, 11, 2, 12, tzinfo=UTC)

    def reserve(i):
        try:
            return ledger.reserve_attempt(run_id=f'r{i}', context={}, now=now)
        except DailyQuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve, range(12)))
    assert sum(x is not None for x in results) == 5
    assert ledger.status(now=now)['application_attempts'] == 5


def test_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / 'quota.json'
    path.write_text('{ broken', encoding='utf-8')
    ledger = YouTubeSearchQuotaLedger(path)
    with pytest.raises(QuotaLedgerError, match='сброс журнала запрещён'):
        ledger.status(now=dt.datetime(2026, 11, 2, 12, tzinfo=UTC))


def test_external_usage_validation(tmp_path):
    ledger = YouTubeSearchQuotaLedger(tmp_path / 'quota.json')
    with pytest.raises(ValueError, match='0–100'):
        ledger.set_external_usage(101, now=dt.datetime(2026, 11, 2, tzinfo=UTC))


def test_ceiling_is_enforced_across_ledger_instances_in_threads(tmp_path):
    path = tmp_path / 'quota.json'
    ledgers = [YouTubeSearchQuotaLedger(path, daily_ceiling=7) for _ in range(4)]
    now = dt.datetime(2026, 11, 2, 12, tzinfo=UTC)

    def reserve(i):
        try:
            return ledgers[i % len(ledgers)].reserve_attempt(
                run_id=f'instance-{i}', context={}, now=now
            )
        except DailyQuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(32)))

    assert sum(value is not None for value in results) == 7
    assert ledgers[0].status(now=now)['application_attempts'] == 7
