"""Headless, independently scheduled blog accounts with shared topic collection."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from blog_account_history import AccountTopicHistory, TopicReservationConflict
from blog_cli_bridge import BlogCliBridge
from blog_controls import BlogWorkflowControls, next_cycle_tick, confirmed_publication
from blog_deadline import CycleBudget, CycleDeadlineExceeded
from blog_preferences import atomic_json_write, automation_config_snapshot
from blog_runtime import access_error_from_exception
from blog_topic_history import topic_key
from blog_workflow import WorkflowReviewRequired
from keyword_database import load_database, update_database, words


class AccountConfigurationError(RuntimeError):
    pass


class AccountStop:
    """The global stop cancels everyone; an account error only stops this worker."""
    def __init__(self, global_stop):
        self.global_stop = global_stop
        self.local_stop = threading.Event()

    def is_set(self):
        return self.global_stop.is_set() or self.local_stop.is_set()

    def set(self):
        self.local_stop.set()

    def clear(self):
        self.local_stop.clear()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.local_stop.wait(.1 if remaining is None else min(.1, remaining))
        return True


class AccountEvents:
    def __init__(self, destination, identifier, label):
        self.destination, self.identifier, self.label = destination, identifier, label

    def put(self, event):
        self.destination.put(('account_event', self.identifier, self.label, event))


def _read_object(path, default=None):
    if not path.exists():
        return copy.deepcopy(default)
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'계정 상태 파일이 객체가 아닙니다: {path.name}')
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


class SharedRealtime:
    """Only the producer fetches. Consumers receive independent snapshots."""
    def __init__(self, fetch, stop, interval, clock, on_groups, log):
        self.fetch, self.stop, self.interval, self.clock = fetch, stop, interval, clock
        self.on_groups, self.log = on_groups, log
        self.condition = threading.Condition()
        self.groups, self.error, self.updated_at = None, None, None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='blog-shared-realtime', daemon=True)
        self.thread.start()

    def run(self):
        tick = self.clock()
        while not self.stop.is_set():
            try:
                groups = self.fetch()
                if (not isinstance(groups, dict) or not any(groups.values())
                        or any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                               for items in groups.values())):
                    raise RuntimeError('공통 실시간 검색어를 확보하지 못했습니다.')
                if self.stop.is_set():
                    break
                self.on_groups(groups)
                with self.condition:
                    self.groups, self.error, self.updated_at = copy.deepcopy(groups), None, self.clock()
                    self.condition.notify_all()
                self.log('공통 실시간 검색어 수집 완료 · 모든 계정이 같은 수집 결과를 공유합니다.')
            except Exception as exc:
                with self.condition:
                    self.error = str(exc)
                    self.condition.notify_all()
                self.log(f'공통 실시간 검색어 갱신 실패: {exc}')
            tick = next_cycle_tick(tick, self.clock(), self.interval)
            if self.stop.wait(max(0, tick - self.clock())):
                break
        with self.condition:
            self.condition.notify_all()

    def get(self, stop, budget=None):
        with self.condition:
            while self.groups is None and self.error is None and not stop.is_set():
                if budget is not None:
                    budget.check(reserve_seconds=600)
                self.condition.wait(.1)
            if stop.is_set():
                raise RuntimeError('사용자가 계정 자동화를 중지했습니다.')
            # A failed refresh must not make yesterday's cache look current.
            if self.groups is None or (self.error and self.updated_at is not None
                                       and self.clock() - self.updated_at >= self.interval):
                raise RuntimeError(self.error or '공통 실시간 검색어를 기다리고 있습니다.')
            return copy.deepcopy(self.groups)


class AccountWorker(BlogWorkflowControls):
    """Plain Python state only: none of the Tk control initializers are called."""
    def __init__(self, runtime, account):
        self.runtime, self.account = runtime, copy.deepcopy(account)
        self.account_id = self.account_owner = account['id']
        self.label = f"{account['id']} · {account['browser']} · {account['blog_id']}"
        from blog_accounts_ui import writer_data_dir
        self.cli_app_dir = writer_data_dir(runtime.root, account)
        self.cli_app_dir.mkdir(parents=True, exist_ok=True)
        self.cli_config_path = self.cli_app_dir / 'runtime-settings.json'
        self.keyword_database_path = runtime.root / 'keywords.json'
        self.full_auto_stop = AccountStop(runtime.stop)
        self.events = AccountEvents(runtime.events, self.account_id, self.label)
        self.topic_history = runtime.history_factory(runtime.root / 'published-topic-history.json', self.account_owner)
        self.cli_preferences = {}
        self.keyword_db, self.cli_article, self._google_search_job = [], None, None
        self._cycle_budget = None
        history = self.cli_app_dir / 'automation-history.json'
        self.auto_history = json.loads(history.read_text(encoding='utf-8')) if history.exists() else []
        if not isinstance(self.auto_history, list):
            raise AccountConfigurationError('계정 자동화 이력을 읽지 못했습니다.')
        enabled_ids = {row.get('id') for row in runtime.accounts
                       if isinstance(row, dict) and row.get('enabled') is True}
        # Keep the existing single-account Whale session. With two accounts the
        # primary also needs the adapter's authenticated blog-ID verification.
        self.owns_browser = not (self.account_id == 'primary' and account['browser'] == 'whale'
                                 and len(enabled_ids) == 1)
        cached = getattr(runtime.app, '_account_login_bots', {}).get((self.account_id, account['browser'], account['blog_id']))
        self.naver_bot = (runtime.app.naver_bot if not self.owns_browser else cached or
            runtime.browser_factory(self.cli_app_dir, self._naver_log, account['browser'], blog_id=account['blog_id']))
        self.naver_bot.reset_stop()
        self._browser_original_log = getattr(self.naver_bot, 'log', None)
        if self._browser_original_log is not None:
            self.naver_bot.log = self._naver_log
        self.cli_bridge = runtime.bridge_factory(self.cli_app_dir, self._naver_log, self.full_auto_stop)
        self._pending_reservation_restored = False
        self._browser_verified = False
        self.thread = None

    def _naver_log(self, message):
        self.runtime.events.put(('account_log', self.account_id, self.label, str(message)))

    def _cli_realtime_groups(self):
        return self.topic_history.filter_groups(self.runtime.realtime.get(self.full_auto_stop, self._cycle_budget),
                                                include_pending=True)

    def _rank_longtail_topics(self, groups, config=None):
        self.keyword_db = words(load_database(self.keyword_database_path))
        # This production method reads plain config/history and emits queue events.
        # Bind it explicitly; never proxy arbitrary app/Tk attributes to a worker.
        return self.runtime.rank_topics(self, groups, config=config)

    def _publish_cli_worker(self, article, config, budget=None):
        if budget is not None and config.get('publish'):
            prior = self._pending_publication_receipt(article, config)
            if prior is not None:
                # No browser submission remains. Preserve the original controls'
                # receipt-first completion even after the time budget or stop.
                self._ensure_google_browser_idle()
                return self._record_cli_publication(article, config, {**prior, 'reused_receipt': True})
        while not self.full_auto_stop.is_set():
            if budget is not None:
                budget.check()
            if self.runtime.publish_lock.acquire(timeout=.1):
                try:
                    if self.full_auto_stop.is_set():
                        raise RuntimeError('발행 대기 중 계정 자동화가 중지되었습니다.')
                    return super()._publish_cli_worker(article, config, budget=budget)
                finally:
                    self.runtime.publish_lock.release()
        raise RuntimeError('발행 대기 중 계정 자동화가 중지되었습니다.')

    def _pending(self):
        return _read_object(self.cli_app_dir / 'pending-blog-topic.json', {})

    def _progress(self, pending):
        choice = pending.get('choice', {})
        identity = {'topic': choice.get('topic'), 'run_dir': pending.get('resume_run_dir') or pending.get('run_dir', '')}
        progress = {'prepared': bool(pending.get('prepared_article')), 'phase': pending.get('phase', '')}
        run = Path(identity['run_dir']).resolve() if identity['run_dir'] else None
        if run is not None and (self.cli_app_dir / 'blog-runs').resolve() in run.parents:
            progress['stages'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted(run.glob('stage-*.checkpoint.json'))}
            editorial = _read_object(run / 'editorial.pending.json', {})
            progress['editorial_article'] = editorial.get('article_sha256')
            manifest = _read_object(run / 'manifest.json', {})
            progress['images'] = [(image.get('paragraph_index'), image.get('sha256'), image.get('approved') is True)
                                  for image in manifest.get('image_candidates', []) if isinstance(image, dict) and image.get('sha256')]
            progress['ready'] = manifest.get('ready_to_publish') is True
        return _hash(identity), _hash(progress)

    def _safe_to_abandon(self, pending, config):
        if (pending.get('publication_started') or pending.get('phase') in {'publishing', 'submitted_uncertain', 'completed'}
                or pending.get('confirmed_receipt') or pending.get('completion_result')):
            return False
        topic = pending.get('choice', {}).get('topic', '')
        if topic_key(topic) in {topic_key(value) for value in self.topic_history.pending_topics()}:
            return False
        prepared = pending.get('prepared_article')
        if isinstance(prepared, dict):
            # A matching browser receipt wins over any stale pending phase.
            reader = getattr(self.naver_bot, 'publication_receipt_for', None)
            if not callable(reader):
                return False
            try:
                return reader(config['blog_id'], self._publication_payload(prepared)) is None
            except Exception:
                return False
        # The workflow cannot submit a preparing/review-held copy before it has
        # produced and checkpointed a publishable article and image identities.
        return pending.get('phase', 'preparing') in {'preparing', 'review_required'}

    def _observe_progress(self, before, after, config, *, before_progress=None):
        path = self.cli_app_dir / 'account-progress.json'
        state = _read_object(path, {})
        if not after.get('choice'):
            atomic_json_write(path, {'version': 1, 'stalled_cycles': 0})
            return False
        identity, progress = self._progress(after)
        before_identity, old_progress = before_progress if before_progress is not None else self._progress(before)
        unchanged = bool(before.get('choice')) and (before_identity, old_progress) == (identity, progress)
        count = (int(state.get('stalled_cycles', 0)) + 1 if state.get('identity') == identity
                 and state.get('progress') == progress else 1) if unchanged else 0
        atomic_json_write(path, {'version': 1, 'identity': identity, 'progress': progress, 'stalled_cycles': count})
        if count < 2 or not self._safe_to_abandon(after, config):
            return False
        archive = self.cli_app_dir / 'abandoned-pending'
        archive.mkdir(exist_ok=True)
        archived = {**copy.deepcopy(after), 'phase': 'abandoned', 'abandoned_at': datetime.now(timezone.utc).isoformat(),
                    'reason': '같은 미제출 원고에서 두 회차 연속 완료 단계·원고·이미지 진전 없음'}
        atomic_json_write(archive / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.json", archived)
        # Keep every run artifact. Archive first, then detach and release only our reservation.
        (self.cli_app_dir / 'pending-blog-topic.json').unlink()
        self.topic_history.release()
        self._naver_log('미제출 원고가 두 회차 연속 진전 없어 자료를 보존해 보관했습니다. 다음 회차에 새 주제를 선택합니다.')
        return True

    def _record_cycle(self, record):
        path = self.cli_app_dir / 'cycle-results.jsonl'
        with path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(record, ensure_ascii=False) + '\n')
            output.flush()
            os.fsync(output.fileno())
        today = datetime.now().astimezone().date()
        records = []
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                item = json.loads(line)
                stamp = datetime.fromisoformat(item['started_at']).astimezone().date()
                if stamp == today:
                    records.append(item)
            except (ValueError, KeyError, TypeError):
                continue
        published = sum(item.get('status') == 'published' for item in records)
        drafts = sum(item.get('status') == 'draft' for item in records)
        summary = (f"오늘 예약 {len(records)}회 · 발행 {published}회 · 발행률 {published / max(1, len(records)):.0%} "
                   f"· 임시저장 {drafts}회 · 이번 회차 {record['status']}")
        self._naver_log(summary + (f" · {record['url']}" if record.get('url') else ''))
        self.events.put(('status', summary))

    def _next_config(self):
        settings = copy.deepcopy(getattr(self.runtime.app, 'settings', {}))
        configured = settings.get('writer_accounts', self.runtime.accounts)
        current = next((value for value in configured if value.get('id') == self.account_id), None)
        if current is None or current.get('enabled') is not True:
            self.full_auto_stop.set()
            return None
        if any(current.get(field) != self.account.get(field) for field in ('blog_id', 'browser')):
            raise AccountConfigurationError('계정 또는 브라우저가 변경되었습니다. 현재 계정을 중지하고 새 설정으로 다시 시작하세요.')
        config = automation_config_snapshot(settings, self.runtime.config)
        config['blog_id'] = self.account['blog_id']
        pending = self._pending()
        saved_id = pending.get('config', {}).get('blog_id')
        if saved_id and saved_id.casefold() != self.account['blog_id'].casefold():
            raise AccountConfigurationError('저장된 미완료 회차의 블로그 ID가 선택한 계정과 다릅니다. 기존 자료를 보존합니다.')
        if not self._pending_reservation_restored:
            self._restore_pending_reservation(pending, config)
            self._pending_reservation_restored = True
        atomic_json_write(self.cli_config_path, config)
        self.cli_preferences = copy.deepcopy(config)
        return config

    def _restore_pending_reservation(self, pending, config):
        if (not pending.get('choice') or pending.get('publication_started')
                or pending.get('confirmed_receipt') or pending.get('completion_result')
                or pending.get('phase') in {'submitted_uncertain', 'completed'}):
            return
        prepared = pending.get('prepared_article')
        if isinstance(prepared, dict):
            # Prepared copies may predate account reservations. Only a successful
            # local receipt lookup proving no submission permits a new reservation.
            if not callable(getattr(self.naver_bot, 'publication_receipt_for', None)):
                raise AccountConfigurationError('준비된 원고의 발행 영수증을 확인하지 못해 기존 자료를 보존합니다.')
            try:
                prior = self._pending_publication_receipt(prepared, config)
            except Exception as exc:
                raise AccountConfigurationError(f'준비된 원고의 발행 영수증을 읽지 못해 기존 자료를 보존합니다: {exc}') from exc
            if prior is not None:
                return
        elif pending.get('phase', 'preparing') not in {'preparing', 'review_required'}:
            return
        choice = pending['choice']
        self.topic_history.reserve(choice['topic'], [choice.get('source_topic', choice['topic']), *choice.get('keywords', [])],
                                   run_dir=pending.get('resume_run_dir') or pending.get('run_dir', ''))

    def _wait_for_next_cycle(self, cycle_tick, finished_at, config):
        scheduled_hours, next_tick = None, None
        while not self.full_auto_stop.is_set():
            settings = copy.deepcopy(getattr(self.runtime.app, 'settings', {}))
            configured = settings.get('writer_accounts', self.runtime.accounts)
            current = next((row for row in configured if row.get('id') == self.account_id), None)
            if current is None or current.get('enabled') is not True:
                self.full_auto_stop.set()
                return None
            latest = automation_config_snapshot(settings, config)
            now = self.runtime.clock()
            if latest['interval_hours'] != scheduled_hours:
                scheduled_hours = latest['interval_hours']
                next_tick = next_cycle_tick(cycle_tick, finished_at, latest['interval_seconds'])
                self.events.put(('status', f"다음 계정 회차까지 {max(0, next_tick - now) / 60:.1f}분 · {scheduled_hours}시간마다"))
            remaining = next_tick - now
            if remaining <= 0:
                return next_tick
            if self.full_auto_stop.wait(min(1, remaining)):
                return None
        return None

    def run(self):
        tick = self.runtime.clock()
        try:
            while not self.full_auto_stop.is_set():
                started = self.runtime.clock()
                budget = self._cycle_budget = self.runtime.budget_factory(limit_seconds=3000, clock=self.runtime.clock)
                record = {'account_id': self.account_id, 'blog_id': self.account['blog_id'],
                          'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'failed'}
                fatal = False
                before, before_progress, config = {}, None, None
                try:
                    config = self._next_config()
                    if config is None:
                        break
                    before = self._pending()
                    previous_completion = _hash(self.auto_history[-1]) if self.auto_history else None
                    # Capture milestones now, rather than re-reading changed files after the cycle.
                    before_progress = self._progress(before)
                    verify = getattr(self.naver_bot, 'verify_account', None)
                    if (not self._browser_verified and callable(verify) and not before.get('publication_started')
                            and before.get('phase') not in {'publishing', 'submitted_uncertain'}
                            and not before.get('confirmed_receipt') and not before.get('completion_result')):
                        budget.check(reserve_seconds=600)
                        try:
                            verify(config['blog_id'])
                        except Exception as exc:
                            raise AccountConfigurationError(f'이 계정의 전용 브라우저 로그인을 확인해 주세요: {exc}') from exc
                        self._browser_verified = True
                    self._naver_log(f"계정 회차 시작 · {config['interval_hours']}시간 간격 · 이번 회차 시간 예산 50분")
                    for conflict_attempt in range(3):
                        try:
                            self._cli_automation_cycle(config, budget=budget)
                            break
                        except TopicReservationConflict:
                            if self._pending().get('choice') or conflict_attempt == 2:
                                raise
                            budget.check(reserve_seconds=600)
                            self._naver_log('다른 계정이 주제를 먼저 예약했습니다. 남은 후보를 다시 선택합니다.')
                    completion = self.auto_history[-1] if self.auto_history and _hash(self.auto_history[-1]) != previous_completion else {}
                    result = completion.get('publication', {})
                    published = confirmed_publication(result)
                    record.update(status=('published' if published else 'draft' if result.get('saved') is True else 'prepared'),
                                  url=result.get('url', '') if published else '',
                                  topic=completion.get('topic', ''), title=completion.get('title', ''),
                                  publication=copy.deepcopy(result))
                except Exception as exc:
                    problem = access_error_from_exception(exc)
                    fatal = isinstance(exc, AccountConfigurationError) or problem is not None
                    record.update(status=('cancelled' if self.full_auto_stop.is_set() else 'access_required' if problem else
                                          'deadline' if isinstance(exc, CycleDeadlineExceeded) else
                                          'review_required' if isinstance(exc, WorkflowReviewRequired) else 'failed'), error=str(exc))
                    self._naver_log(f"계정 회차 {record['status']}: {exc}")
                    self.events.put(('auto_error', str(exc)))
                    if problem:
                        self.events.put(('cli_access_required', problem, False))
                finally:
                    record['elapsed_seconds'] = max(0, self.runtime.clock() - started)
                    record['finished_at'] = datetime.now(timezone.utc).isoformat()
                    if config is not None and not fatal and not self.full_auto_stop.is_set():
                        try:
                            after = self._pending()
                            # Pass the captured snapshot to avoid a mutable-file false equivalence.
                            record['abandoned'] = self._observe_progress(before, after, config, before_progress=before_progress)
                            record['pending_topic'] = after.get('choice', {}).get('topic', '')
                        except Exception as exc:
                            record['state_error'] = str(exc)
                            fatal = True
                            self._naver_log(f'계정 상태 보존 실패 · 이 계정만 중지합니다: {exc}')
                    try:
                        self._record_cycle(record)
                    except OSError as exc:
                        fatal = True
                        self._naver_log(f'계정 회차 결과를 저장하지 못해 이 계정을 중지합니다: {exc}')
                if fatal or self.full_auto_stop.is_set():
                    break
                tick = self._wait_for_next_cycle(tick, self.runtime.clock(), config)
                if tick is None:
                    break
                job = getattr(self, '_google_search_job', None)
                if job is None or not job.alive():
                    self.naver_bot.reset_stop()
        finally:
            self.close()

    def close(self):
        self.full_auto_stop.set()
        job = getattr(self, '_google_search_job', None)
        if job is not None:
            try:
                job.close()
            except Exception as exc:
                self._naver_log(f'계정 브라우저 작업 종료를 기다립니다: {exc}')
                while job.alive():
                    job.local_stop.set()
                    job.thread.join(.2)
        try:
            if self.owns_browser:
                self.naver_bot.close()
        finally:
            if self._browser_original_log is not None:
                self.naver_bot.log = self._browser_original_log
            self.events.put(('account_stopped',))


class AccountsRuntime:
    def __init__(self, app, config, *, browser_factory=None, bridge_factory=BlogCliBridge,
                 history_factory=AccountTopicHistory, fetch_groups=None, worker_factory=AccountWorker,
                 budget_factory=CycleBudget, clock=time.monotonic, interval_seconds=3600):
        self.app, self.config = app, copy.deepcopy(config)
        self.root = Path(app.cli_app_dir).resolve()
        self.events, self.stop = app.events, app.full_auto_stop
        self.clock, self.interval, self.budget_factory = clock, interval_seconds, budget_factory
        self.bridge_factory, self.history_factory, self.worker_factory = bridge_factory, history_factory, worker_factory
        if browser_factory is None:
            from blog_browser import create_blog_browser
            browser_factory = create_blog_browser
        self.browser_factory = browser_factory
        from picture_cleaner_pc import PictureCleanerApp, fetch_realtime_groups
        self.rank_topics = getattr(type(app), '_rank_longtail_topics', PictureCleanerApp._rank_longtail_topics)
        self.accounts = copy.deepcopy(getattr(app, 'settings', {}).get('writer_accounts', config.get('writer_accounts', [])))
        self.publish_lock = threading.Lock()
        self.workers = []
        self.realtime = SharedRealtime(fetch_groups or fetch_realtime_groups, AccountStop(self.stop), self.interval, self.clock,
                                       self._observed_groups, self._log)

    def _log(self, message):
        self.events.put(('account_log', 'shared', '공통', str(message)))

    def _observed_groups(self, groups):
        history = self.history_factory(self.root / 'published-topic-history.json', 'realtime-producer')
        filtered = history.filter_groups(groups, include_pending=True)
        update_database(self.root / 'keywords.json', observed=[word for values in filtered.values() for word in values],
                        keyword_filter=lambda values: history.filter_keywords(values, include_pending=True))
        self.events.put(('realtime_groups', filtered))

    def _valid_accounts(self):
        seen_ids, seen_blogs = set(), set()
        for account in self.accounts if isinstance(self.accounts, list) else []:
            if not isinstance(account, dict) or account.get('enabled') is not True:
                continue
            identifier, browser, blog_id = account.get('id'), account.get('browser'), account.get('blog_id')
            if (identifier not in {'primary', 'secondary'} or identifier in seen_ids
                    or browser not in ({'whale', 'chrome', 'edge'} if identifier == 'primary' else {'chrome', 'edge'})
                    or not isinstance(blog_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', blog_id)
                    or blog_id.casefold() in seen_blogs):
                self.events.put(('account_log', str(identifier), '계정 설정', '잘못되거나 중복된 계정 설정으로 이 계정의 시작을 생략했습니다.'))
                continue
            seen_ids.add(identifier)
            seen_blogs.add(blog_id.casefold())
            yield account

    def run(self):
        try:
            self.app._account_bots = {}
            # Restore all saved reservations before any new topic selection can
            # race them. Legacy primary data receives first claim on startup.
            for account in sorted(self._valid_accounts(), key=lambda row: row['id'] != 'primary'):
                if self.stop.is_set():
                    break
                worker = None
                try:
                    worker = self.worker_factory(self, account)
                    if worker._next_config() is None:
                        worker.close()
                        continue
                    self.workers.append(worker)
                    self.app._account_bots[account['id']] = worker.naver_bot
                except Exception as exc:
                    if worker is not None:
                        worker.close()
                    self.events.put(('account_log', account['id'], account['id'], f'계정 시작 실패 · 다른 계정은 계속 실행합니다: {exc}'))
            self.realtime.start()  # Independent of whether primary resumes a pending article.
            for worker in self.workers:
                if self.stop.is_set():
                    worker.close()
                    continue
                try:
                    worker.thread = threading.Thread(target=worker.run, name='blog-account-' + worker.account_id, daemon=True)
                    worker.thread.start()
                except Exception as exc:
                    worker.thread = None
                    worker.close()
                    worker._naver_log(f'계정 실행 스레드를 시작하지 못했습니다: {exc}')
            while any(worker.thread and worker.thread.is_alive() for worker in self.workers):
                if self.stop.is_set():
                    for worker in self.workers:
                        worker.full_auto_stop.set()
                        worker.naver_bot.stop()
                for worker in self.workers:
                    if worker.thread:
                        worker.thread.join(.1)
        finally:
            # The producer uses a private stop so an account failure never sets
            # the user's global event or accidentally cancels unrelated manual work.
            self.realtime.stop.set()
            if self.realtime.thread:
                self.realtime.thread.join()
            self.app.full_auto_active = self.app.naver_task_active = False
            self.events.put(('cli_idle',))
            self.events.put(('status', '모든 계정 자동화가 중지되었습니다.'))


def run_accounts(app, config):
    runtime = AccountsRuntime(app, config)
    app._accounts_runtime = runtime
    runtime.run()
