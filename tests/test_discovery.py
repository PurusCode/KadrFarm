import datetime as dt
import json
from pathlib import Path

import pytest

from kadr_farm.config import Settings
from kadr_farm.discovery import (
    DEFAULT_MAX_PAGES, DEFAULT_MAX_RESULTS, SEARCH_ORDERS, TOPIC_STREAMS,
    discover_youtube, purge_expired_discovery_output,
)
from kadr_farm.discovery_quota import DailyQuotaExceeded, YouTubeSearchQuotaLedger


def _settings(api_key="test-key", *, data_dir=None):
    return Settings(
        root=Path('.'), data_dir=data_dir or Path('data'), output_dir=Path('output'),
        cache_dir=Path('cache'), broll_dir=Path('broll'), images_dir=Path('images'),
        audio_dir=Path('audio'), ffmpeg='ffmpeg', ffprobe='ffprobe',
        width=1920, height=1080, fps=25, bitrate='4500k', gop=50,
        youtube_api_key=api_key, pexels_api_key='', pixabay_api_key='',
        tts_engine='piper', tts_language='ru', tts_speaker='ru_RU-dmitri-medium',
        tts_speed=0.9, providers=[],
    )


def _fake_api(calls, *, return_next=True):
    search_number = 0

    def fake_get(path, params, api_key):
        nonlocal search_number
        calls.append((path, dict(params), api_key))
        if path == 'search':
            search_number += 1
            page = params.get('pageToken')
            video_id = "video-shared" if search_number <= 2 else f"video-{search_number}-{('p2' if page else 'p1')}"
            return {
                'pageInfo': {'totalResults': 2},
                'nextPageToken': f"next-{search_number}" if return_next and not page else None,
                'items': [{
                    'id': {'kind': 'youtube#video', 'videoId': video_id},
                    'snippet': {
                        'title': f'Title {video_id}', 'channelId': 'channel-1',
                        'channelTitle': 'Channel', 'publishedAt': '2026-11-02T08:00:00Z',
                    },
                }],
            }
        if params.get('chart') == 'mostPopular':
            return {'items': [{
                'id': f"popular-{params['regionCode']}",
                'snippet': {'title': 'Popular chart item', 'channelId': 'c',
                            'channelTitle': 'Popular channel',
                            'publishedAt': '2026-11-02T07:00:00Z', 'categoryId': '20'},
                'statistics': {'viewCount': '5000'},
                'contentDetails': {'duration': 'PT8M'},
            }]}
        items = []
        for video_id in params['id'].split(','):
            items.append({
                'id': video_id,
                'snippet': {'title': f'Title {video_id}', 'channelId': 'c',
                            'channelTitle': 'Channel',
                            'publishedAt': '2026-11-02T08:00:00Z', 'categoryId': '28'},
                'statistics': {'viewCount': '12345', 'likeCount': '321', 'commentCount': '20'},
                'contentDetails': {'duration': 'PT5M20S'},
            })
        return {'items': items}
    return fake_get


def _now():
    return dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)


def test_default_discovery_searches_both_orders_with_80_first_pages(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('kadr_farm.discovery._get', _fake_api(calls))
    settings = _settings(data_dir=tmp_path / 'data')
    report = discover_youtube(settings, tmp_path / 'output', now=_now())

    search_calls = [c for c in calls if c[0] == 'search']
    assert len(TOPIC_STREAMS) == 10
    assert SEARCH_ORDERS == ('viewCount', 'date')
    assert DEFAULT_MAX_RESULTS == 50
    assert DEFAULT_MAX_PAGES == 1
    assert len(search_calls) == 80
    assert len(report['search_sets']) == 80
    assert {x['order'] for x in report['search_sets']} == {'viewCount', 'date'}
    assert all(x['pages_fetched'] == 1 and x['has_more_pages'] for x in report['search_sets'])
    assert all(c[1]['maxResults'] == 50 for c in search_calls)
    assert all(c[1]['order'] in SEARCH_ORDERS for c in search_calls)
    assert all('pageToken' not in c[1] for c in search_calls)

    ru = next(c[1] for c in search_calls if c[1].get('regionCode') == 'RU')
    global_search = next(c[1] for c in search_calls if 'regionCode' not in c[1])
    assert ru['relevanceLanguage'] == 'ru'
    assert global_search['relevanceLanguage'] == 'en'

    # Details were fetched in batches; chart remains a separate supplemental source.
    candidate = report['unique_candidates'][0]
    assert candidate['view_count'] == 12345
    assert candidate['like_count'] == 321
    assert candidate['duration'] == 'PT5M20S'
    assert isinstance(candidate['found_in'], list)
    assert {'viewCount', 'date'} <= {x['order'] for x in candidate['found_in'] if x.get('stream_id') != 'youtube_most_popular_chart'}
    assert all('query' in x for x in candidate['found_in'] if x.get('stream_id') != 'youtube_most_popular_chart')
    assert report['candidate_count'] == len(report['unique_candidates'])
    assert len(report['most_popular_charts']['RU']['videos']) == 1
    assert report['quota_usage']['search_list_calls'] == 80
    assert report['quota_usage']['search_list_calls_remaining_today'] == 10
    assert report['quota_usage']['search_list_daily_accounting']['timezone'] == 'America/Los_Angeles'
    assert report['search_design']['orders'] == ['viewCount', 'date']
    assert report['search_design']['order'] == 'viewCount'  # legacy schema 2.0 field
    assert report['search_design']['search_call_budget'] == 90
    assert report['schema_version'] == '2.0'

    saved = json.loads((tmp_path / 'output' / 'discovery.json').read_text(encoding='utf-8'))
    assert saved['candidate_count'] == report['candidate_count']
    md = (tmp_path / 'output' / 'discovery.md').read_text(encoding='utf-8')
    assert 'order=viewCount' in md and 'order=date' in md
    assert 'скорость прироста' in md.lower()


def test_custom_query_uses_8_first_pages_and_preserves_order_provenance(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('kadr_farm.discovery._get', _fake_api(calls, return_next=False))
    report = discover_youtube(
        _settings(data_dir=tmp_path / 'data'), tmp_path / 'out',
        query='локальная нейросеть|local AI', now=_now(),
    )
    search_calls = [c for c in calls if c[0] == 'search']
    assert len(search_calls) == 8
    assert len(report['search_sets']) == 8
    assert {x['order'] for x in report['search_sets']} == {'viewCount', 'date'}
    assert all('локальная нейросеть' in x['query'] for x in report['search_sets'])
    assert report['quota_usage']['search_list_calls_remaining_today'] == 82


def test_second_full_run_is_blocked_before_any_new_api_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('kadr_farm.discovery._get', _fake_api(calls))
    settings = _settings(data_dir=tmp_path / 'data')
    discover_youtube(settings, tmp_path / 'first', now=_now())
    previous_search_count = len([x for x in calls if x[0] == 'search'])
    with pytest.raises(DailyQuotaExceeded, match='нужно 80 первых страниц'):
        discover_youtube(settings, tmp_path / 'second', now=_now())
    assert len([x for x in calls if x[0] == 'search']) == previous_search_count
    assert not (tmp_path / 'second' / 'discovery.json').exists()


def test_external_usage_reconciliation_is_persistent_and_reduces_daily_capacity(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('kadr_farm.discovery._get', _fake_api(calls, return_next=False))
    settings = _settings(data_dir=tmp_path / 'data')
    ledger = YouTubeSearchQuotaLedger(settings.data_dir / 'usage' / 'youtube_search_quota.json')
    ledger.set_external_usage(84, now=_now())
    assert ledger.status(now=_now())['remaining'] == 6
    with pytest.raises(DailyQuotaExceeded, match='осталось только 6'):
        discover_youtube(settings, tmp_path / 'out', query='test', now=_now())
    assert not [x for x in calls if x[0] == 'search']


def test_optional_pagination_uses_only_budget_after_all_first_pages(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('kadr_farm.discovery._get', _fake_api(calls, return_next=True))
    report = discover_youtube(
        _settings(data_dir=tmp_path / 'data'), tmp_path / 'out',
        query='test query', max_pages=2, search_call_budget=10, now=_now(),
    )
    search_calls = [x for x in calls if x[0] == 'search']
    assert len(search_calls) == 10
    assert sum(1 for x in search_calls if 'pageToken' in x[1]) == 2
    assert sum(1 for x in report['search_sets'] if x['pages_fetched'] == 2) == 2
    assert report['search_budget_exhausted'] is True


def test_failed_search_attempt_is_counted_before_http_failure(tmp_path, monkeypatch):
    def failed_get(path, params, api_key):
        if path == 'search':
            raise RuntimeError('HTTP 403 (quotaExceeded)')
        return _fake_api([])(path, params, api_key)
    monkeypatch.setattr('kadr_farm.discovery._get', failed_get)
    settings = _settings(data_dir=tmp_path / 'data')
    with pytest.raises(RuntimeError, match='quotaExceeded'):
        discover_youtube(settings, tmp_path / 'out', query='test', now=_now())
    status = YouTubeSearchQuotaLedger(settings.data_dir / 'usage' / 'youtube_search_quota.json').status(now=_now())
    assert status['application_attempts'] == 1
    raw = json.loads((settings.data_dir / 'usage' / 'youtube_search_quota.json').read_text(encoding='utf-8'))
    record = raw['days'][status['pacific_date']]['attempts'][0]
    assert record['outcome'] == 'failed'


def test_validation(tmp_path):
    with pytest.raises(RuntimeError, match='YOUTUBE_API_KEY'):
        discover_youtube(_settings(api_key=''), tmp_path)
    with pytest.raises(ValueError, match='1 до 50'):
        discover_youtube(_settings(data_dir=tmp_path / 'a'), tmp_path, max_results=51)
    with pytest.raises(ValueError, match='1 до 3'):
        discover_youtube(_settings(data_dir=tmp_path / 'b'), tmp_path, max_pages=4)
    with pytest.raises(ValueError, match='1 до 100'):
        discover_youtube(_settings(data_dir=tmp_path / 'c'), tmp_path, search_call_budget=101)
    with pytest.raises(ValueError, match='не должен быть пустым'):
        discover_youtube(_settings(data_dir=tmp_path / 'd'), tmp_path, query='  ')
    with pytest.raises(ValueError, match='timezone-aware'):
        discover_youtube(_settings(data_dir=tmp_path / 'e'), tmp_path, now=dt.datetime(2026, 11, 2))


def test_cleanup_removes_discovery_stage3_and_stage4_outputs_only_after_expiry(tmp_path):
    report = {'refresh_or_delete_by_utc': '2026-11-01T00:00:00Z'}
    (tmp_path / 'discovery.json').write_text(json.dumps(report), encoding='utf-8')
    for name in ('discovery.md', 'topics.json', 'topics.md', 'editor.html', 'editorial.json'):
        (tmp_path / name).write_text('api data', encoding='utf-8')
    keep = tmp_path / 'keep.txt'
    keep.write_text('keep', encoding='utf-8')
    assert purge_expired_discovery_output(tmp_path, now=_now()) is True
    for name in ('discovery.json', 'discovery.md', 'topics.json', 'topics.md', 'editor.html', 'editorial.json'):
        assert not (tmp_path / name).exists()
    assert keep.read_text(encoding='utf-8') == 'keep'


def test_cleanup_keeps_unexpired_report(tmp_path):
    (tmp_path / 'discovery.json').write_text(
        json.dumps({'refresh_or_delete_by_utc': '2026-12-01T00:00:00Z'}), encoding='utf-8'
    )
    assert purge_expired_discovery_output(tmp_path, now=_now()) is False
    assert (tmp_path / 'discovery.json').exists()
