"""Read the user-selected public static keyword snapshot only on queue exhaustion."""
from __future__ import annotations

import copy
import json
import re
import threading
import time

import requests

from blog_preferences import blocked_term_hits
from blog_topic_history import normalize_topic, topic_key


SITE_URL = 'https://dicacros-gif.github.io/rt/'
# Declared by that page's load() function; this is its public static asset,
# not the /api endpoints also present in the site's optional local-server code.
SNAPSHOT_URL = SITE_URL + 'data/trends.json'
SOURCE_NAMES = {'daum': 'RT · 다음', 'google': 'RT · Google', 'naver': 'RT · 네이버',
                'signal': 'RT · 시그널'}
INTENT_FAMILIES = {
    '방법·절차': ('방법', '절차', '신청', '설정', '사용법', '확인법'),
    '조건·대상': ('조건', '대상', '자격', '기준', '준비', '서류'),
    '비교·선택': ('차이', '비교', '선택', '추천', '장단점'),
    '이유·원리': ('이유', '원인', '원리', '왜', '뜻', '의미'),
    '비용·기간': ('비용', '가격', '요금', '기간', '언제', '얼마'),
    '문제·관리': ('오류', '해결', '관리', '보관', '수리', '예방'),
}
ONE_OFF = re.compile(r'속보|사퇴|당선|우승|승리|패배|당첨번호|정답|실시간|긴급속보|오늘만|이번\s*회차')


def intent_features(topic, keywords):
    text = ' '.join([topic, *keywords])
    families = [name for name, cues in INTENT_FAMILIES.items() if any(cue in text for cue in cues)]
    durable_families = [name for name in families if name != '비용·기간']
    one_off = bool(ONE_OFF.search(topic))
    return {'intent_families': families, 'intent_diversity_bonus': min(24, len(families) * 4),
            'durability_bonus': min(24, len(durable_families) * 6), 'one_off_penalty': 22 if one_off else 0}


def parse_snapshot(data):
    if not isinstance(data, dict) or not isinstance(data.get('portals'), list):
        raise ValueError('RT 정적 검색어 파일의 portals 형식이 올바르지 않습니다.')
    related_data = data.get('related') if isinstance(data.get('related'), dict) else {}
    groups, related = {}, {}
    for portal in data['portals']:
        if not isinstance(portal, dict) or portal.get('id') not in SOURCE_NAMES or not isinstance(portal.get('items'), list):
            continue
        label = SOURCE_NAMES[portal['id']]
        words, seen = [], set()
        for item in portal['items'][:5000]:
            if not isinstance(item, dict) or not isinstance(item.get('keyword'), str):
                continue
            word = normalize_topic(item['keyword'])
            key = topic_key(word)
            if not key or key in seen or len(word) > 150:
                continue
            seen.add(key)
            words.append(word)
            record = related_data.get(str(item.get('id')), related_data.get(word, {}))
            if not isinstance(record, dict):
                continue
            destination = related.setdefault(word, {})
            for field, name in (('fullItems', 'RT 전체 연관어'), ('prefixItems', 'RT 첫 단어 연관어')):
                items = record.get(field, [])
                if not isinstance(items, list):
                    continue
                values = destination.setdefault(name, [])
                for value in items[:300]:
                    keyword = value.get('keyword') if isinstance(value, dict) else None
                    if isinstance(keyword, str):
                        keyword = normalize_topic(keyword)
                        if keyword and len(keyword) <= 150 and keyword not in values:
                            values.append(keyword)
        groups[label] = words
    if not any(groups.values()):
        raise ValueError('RT 사이트의 정적 파일에 사용 가능한 검색어 목록이 없습니다.')
    return {'groups': groups, 'related': related, 'updated_at': str(data.get('updatedAt', '')),
            'source_url': SITE_URL}


def fetch_snapshot():
    with requests.get(SNAPSHOT_URL, timeout=(5, 15), stream=True) as response:
        response.raise_for_status()
        parts, total = [], 0
        for part in response.iter_content(65536):
            total += len(part)
            if total > 16 * 1024 * 1024:
                raise ValueError('RT 정적 파일이 16MB 한도를 넘었습니다.')
            parts.append(part)
    return parse_snapshot(json.loads(b''.join(parts).decode('utf-8-sig')))


class SharedFallbackSnapshot:
    """All account threads share one bounded fetch; filtering happens after it."""
    def __init__(self, fetch=fetch_snapshot, clock=time.monotonic, ttl=3600):
        self.fetch, self.clock, self.ttl = fetch, clock, ttl
        self.lock = threading.Lock()
        self.snapshot, self.updated_at, self.error, self.failed_at = None, None, None, None

    def get(self, stop=None):
        while not self.lock.acquire(timeout=.1):
            if stop is not None and stop.is_set():
                raise RuntimeError('사용자가 작업을 중지했습니다.')
        try:
            if stop is not None and stop.is_set():
                raise RuntimeError('사용자가 작업을 중지했습니다.')
            now = self.clock()
            if self.snapshot is not None and now - self.updated_at < self.ttl:
                return copy.deepcopy(self.snapshot)
            if self.error and self.failed_at is not None and now - self.failed_at < 300:
                raise RuntimeError(self.error)
            try:
                snapshot = self.fetch()
            except Exception as exc:
                self.error, self.failed_at = str(exc), now
                raise
            self.snapshot, self.updated_at, self.error = copy.deepcopy(snapshot), self.clock(), None
            return copy.deepcopy(snapshot)
        finally:
            self.lock.release()


_SHARED = SharedFallbackSnapshot()


def shared_snapshot(stop=None):
    return _SHARED.get(stop)


def rank_fallback(snapshot, history, config, ranker, ephemeral, *, limit=24):
    """Reuse site-provided related words; never request per-keyword autocomplete."""
    groups = history.filter_groups(snapshot['groups'], include_pending=True)
    blocked = config.get('blocked_terms')
    groups = {source: [word for word in words if not blocked_term_hits(word, blocked) and not ephemeral(word)]
              for source, words in groups.items()}
    eligible = {word for words in groups.values() for word in words}
    related = {}
    for word in eligible:
        data = snapshot['related'].get(word, {})
        if not isinstance(data, dict) or blocked_term_hits(data, blocked):
            continue
        related[word] = {source: history.filter_keywords(values, include_pending=True) for source, values in data.items()}
    ranked = ranker(groups, related, exclude_topics=history.blocked_topics(), blocked_terms=blocked)
    selected = []
    for candidate in ranked:
        if history.is_duplicate(candidate['topic'], candidate['keywords'], candidate['topic'],
                keyword_threshold=config.get('duplicate_keyword_threshold', .4),
                title_threshold=config.get('duplicate_title_threshold', .5)):
            continue
        candidate['fallback_source'] = SITE_URL
        candidate['fallback_updated_at'] = snapshot.get('updated_at', '')
        selected.append(candidate)
        if len(selected) >= limit:
            break
    chosen = {candidate['topic'] for candidate in selected}
    return selected, {word: value for word, value in related.items() if word in chosen}, {
        source: [word for word in words if word in chosen] for source, words in groups.items()}
