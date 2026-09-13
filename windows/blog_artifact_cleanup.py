"""Retry verified publication cleanup without using age as proof of publication.

The application's existing instance lock owns this queue. A shared thread lock
also serializes publication callbacks and the between-cycle maintenance hook.
History and browser receipts remain outside every deletion target.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time

from blog_preferences import atomic_json_write
from blog_topic_history import TopicHistory, TopicHistoryError, _confirmed, _published_url, topic_key


class CleanupError(RuntimeError):
    pass


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_PLANS = ("topic-review-", "google-search-plan-")
_RUN_FILES = ("request.json", "manifest.json", "google-search-checkpoint.json")
_DRAFT_FILES = ("request.json", "manifest.json", "editorial.pending.json")


def _overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def _read_json(path):
    if path.resolve() != Path(os.path.abspath(path)):
        raise CleanupError(f"정리 근거 파일의 연결 경로를 차단합니다: {path.name}")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise CleanupError(f"정리 근거 파일이 너무 큽니다: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CleanupError(f"정리 근거 파일의 형식이 올바르지 않습니다: {path.name}")
    return value


def _record_paths(value):
    """Read only known artifact fields, never arbitrary prose/URL strings."""
    paths = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"run_dir", "resume_run_dir", "selection_run_dir", "path", "original_path"}:
                if isinstance(child, str) and child.strip():
                    paths.add(Path(child).resolve())
            elif key == "auxiliary_dirs":
                if not isinstance(child, list) or any(not isinstance(item, str) for item in child):
                    raise CleanupError("보존할 보조 자료 경로의 형식이 올바르지 않습니다.")
                paths.update(Path(item).resolve() for item in child if item.strip())
            elif isinstance(child, (dict, list)):
                paths.update(_record_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.update(_record_paths(child))
    return paths


class ArtifactCleanup:
    def __init__(self, app_dir, log=lambda message: None, history=None, clock=time.time):
        self.root = Path(app_dir).resolve()
        self.path = self.root / "artifact-cleanup-queue.json"
        self.history = history or TopicHistory(self.root / "published-topic-history.json")
        self.log, self.clock = log, clock
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(os.path.normcase(str(self.root)), threading.RLock())

    def _load(self):
        data = _read_json(self.path) if self.path.exists() else {"version": 1, "entries": {}, "blocked_paths": []}
        if (data.get("version") != 1 or not isinstance(data.get("entries"), dict)
                or not isinstance(data.get("blocked_paths"), list)
                or any(not isinstance(item, str) or not item for item in data["blocked_paths"])):
            raise CleanupError("산출물 정리 대기열을 읽을 수 없어 모든 자료를 보존합니다.")
        for entry in data["entries"].values():
            if (not isinstance(entry, dict) or entry.get("kind") not in {"publication", "discarded_plan"}
                    or entry.get("status") not in {"pending", "done", "blocked"}
                    or not isinstance(entry.get("targets"), list)):
                raise CleanupError("산출물 정리 대기열 항목이 손상되어 자료를 보존합니다.")
            for target in entry["targets"]:
                if (not isinstance(target, dict) or not isinstance(target.get("path"), str)
                        or target.get("status") not in {"pending", "done", "blocked"}
                        or not isinstance(target.get("identity"), list) or len(target["identity"]) != 2
                        or any(type(item) is not int for item in target["identity"])
                        or type(target.get("attempts")) is not int
                        or not isinstance(target.get("retry_at"), (int, float))):
                    raise CleanupError("산출물 정리 대상 기록이 손상되어 자료를 보존합니다.")
        return data

    def _policy_paths(self, data):
        blocked = set(data["blocked_paths"])

        def collect(value):
            if isinstance(value, dict):
                if value.get("cleanup_blocked_by_policy") is True:
                    raw = value.get("cleanup_blocked_run")
                    if not isinstance(raw, str) or not raw.strip():
                        raise CleanupError("삭제 차단 기록의 대상 경로가 없어 자료를 보존합니다.")
                    blocked.add(str(Path(raw).resolve()))
                for key in ("cleanup_blocked_paths", "cleanup_preserved_paths"):
                    if key in value:
                        items = value[key]
                        if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
                            raise CleanupError("산출물 보존 목록을 읽을 수 없습니다.")
                        blocked.update(str(Path(item).resolve()) for item in items)
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        for path in (self.root / "logs" / "monitor-state.json", self.root / "cleanup-preserve.json"):
            if path.exists():
                collect(_read_json(path))
        data["blocked_paths"] = sorted(blocked)
        return {Path(item).resolve() for item in blocked}

    def _target(self, raw, run=None, plan_only=False):
        if not isinstance(raw, str) or not raw.strip():
            raise CleanupError("산출물 정리 대상 경로가 없습니다.")
        lexical = Path(os.path.abspath(raw))
        path = lexical.resolve()
        runs, google = self.root / "blog-runs", self.root / "google-reference-candidates"
        if path != lexical or runs.resolve() != runs or google.resolve() != google:
            raise CleanupError(f"산출물 정리 연결 경로 차단: {raw}")
        plan = path.parent == runs and path.name.startswith(_PLANS)
        allowed = plan if plan_only else (path == run and path.parent == runs) or plan or google in path.parents
        if not allowed or path in {self.root, runs, google}:
            raise CleanupError(f"산출물 정리 경로 차단: {raw}")
        if plan and any((path / name).exists() for name in ("request.json", "manifest.json", "editorial.pending.json")):
            raise CleanupError("원고 자료가 있는 폴더는 폐기된 검색 계획으로 취급하지 않습니다.")
        if path.exists() and not path.is_dir():
            raise CleanupError(f"산출물 정리 대상이 폴더가 아닙니다: {path}")
        return path

    def _references(self, exclude_run=None):
        """Preserve references from *all* retained runs, including failed ones."""
        references = set()
        pending = self.root / "pending-blog-topic.json"
        if pending.exists():
            data = _read_json(pending)
            own_run = data.get("run_dir") or data.get("resume_run_dir")
            if not (exclude_run and isinstance(own_run, str) and Path(own_run).resolve() == exclude_run):
                references.update(_record_paths(data))
        runs = self.root / "blog-runs"
        for run in runs.iterdir() if runs.is_dir() else ():
            if not run.is_dir() or run.resolve() == exclude_run:
                continue
            if run.resolve() != run:
                raise CleanupError("연결된 회차 폴더의 보존 자료를 확인할 수 없습니다.")
            if run.name.startswith(_PLANS) and not any((run / name).exists() for name in _DRAFT_FILES):
                continue
            references.add(run.resolve())
            for name in _RUN_FILES:
                path = run / name
                if path.exists():
                    references.update(_record_paths(_read_json(path)))
        return references

    def _verify_publication(self, entry):
        run = self._target(entry.get("run_dir"), Path(str(entry.get("run_dir", ""))).resolve())
        if run.parent != self.root / "blog-runs" or run.name.startswith(_PLANS):
            raise CleanupError("발행 원고 회차 폴더가 아닌 경로는 보존합니다.")
        key = entry.get("article_key")
        if not isinstance(key, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", key):
            raise CleanupError("게시 영수증의 원고 식별자가 없어 산출물을 보존합니다.")
        receipt = _read_json(self.root / "publication_receipts" / f"{key}.json")
        if (not _confirmed(receipt) or receipt.get("article_key") != key or receipt.get("content_verified") is not True
                or _published_url(receipt.get("url")) != entry.get("url")):
            raise CleanupError("게시 본문 확인 영수증이 일치하지 않아 산출물을 보존합니다.")
        with self.history._locked():
            recorded = self.history._read()["published"].get(topic_key(entry.get("topic", "")))
        if (not isinstance(recorded, dict) or _published_url(recorded.get("url")) != entry.get("url")
                or not recorded.get("run_dir") or Path(recorded["run_dir"]).resolve() != run
                or recorded.get("article_key") != key):
            raise CleanupError("저장된 발행 이력과 정리 요청이 일치하지 않아 산출물을 보존합니다.")
        if "draft_files" in entry and run.exists():
            fingerprints = entry["draft_files"]
            if not isinstance(fingerprints, dict) or any(name not in _DRAFT_FILES for name in fingerprints):
                raise CleanupError("정리 대기열의 원고 지문 형식이 올바르지 않습니다.")
            started = any(target.get("attempts", 0) > 0 and Path(target.get("path", "")) == run
                          for target in entry.get("targets", []))
            for name in _DRAFT_FILES:
                path = run / name
                if path.is_file():
                    if fingerprints.get(name) != hashlib.sha256(path.read_bytes()).hexdigest():
                        raise CleanupError("정리 대기 중 원고가 변경되어 자료를 보존합니다.")
                elif name in fingerprints and not started:
                    raise CleanupError("정리 대기 중 원고 근거 파일이 없어져 자료를 보존합니다.")
        return run

    def _new_target(self, path):
        if not path.exists():
            return None
        stat = path.stat()
        return {"path": str(path), "identity": [stat.st_dev, stat.st_ino], "status": "pending", "attempts": 0, "retry_at": 0}

    def enqueue_publication(self, article):
        result = article.get("publication")
        if not _confirmed(result) or result.get("content_verified") is not True:
            raise CleanupError("게시 URL과 본문 확인이 모두 완료된 자료만 정리할 수 있습니다.")
        entry = {"kind": "publication", "status": "pending", "topic": article.get("topic"),
                 "run_dir": article.get("run_dir"), "article_key": result.get("article_key"),
                 "url": _published_url(result.get("url")), "created_at": self.clock(), "targets": []}
        with self.lock:
            data = self._load()
            blocked = self._policy_paths(data)
            run = self._verify_publication(entry)
            identity = hashlib.sha256((str(run) + "\n" + entry["url"]).encode("utf-8")).hexdigest()
            if identity not in data["entries"]:
                entry["draft_files"] = {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                                        for name in _DRAFT_FILES if (run / name).is_file()}
                values = [str(run), *article.get("auxiliary_dirs", [])]
                for name in _RUN_FILES:
                    path = run / name
                    if path.exists():
                        # Explicit folder references also survive a partial browser/search failure.
                        def folders(value):
                            if isinstance(value, dict):
                                for key, child in value.items():
                                    if key == "auxiliary_dirs" and isinstance(child, list):
                                        values.extend(child)
                                    elif key == "selection_run_dir" and isinstance(child, str):
                                        values.append(child)
                                    elif isinstance(child, (dict, list)):
                                        folders(child)
                            elif isinstance(value, list):
                                for child in value:
                                    folders(child)
                        saved = _read_json(path)
                        folders(saved)
                        google_root = self.root / "google-reference-candidates"
                        for referenced in _record_paths(saved):
                            if google_root in referenced.parents and referenced.is_file():
                                values.append(str(referenced.parent))
                for raw in dict.fromkeys(values):
                    path = self._target(raw, run)
                    target = self._new_target(path)
                    if target:
                        if any(_overlaps(run, item) or _overlaps(path, item) for item in blocked):
                            target["status"] = "blocked"
                            target["last_error"] = "삭제 차단 정책에 따라 보존"
                        entry["targets"].append(target)
                # Delete children before the run so external auxiliary paths remain independently retryable.
                entry["targets"].sort(key=lambda item: len(Path(item["path"]).parts), reverse=True)
                data["entries"][identity] = entry
            atomic_json_write(self.path, data)  # Persist the request before any deletion.

    def enqueue_discarded_plan(self, raw):
        with self.lock:
            data = self._load()
            self._policy_paths(data)
            path = self._target(raw, plan_only=True)
            target = self._new_target(path)
            if target is None:
                return
            identity = hashlib.sha256(("discarded-plan\n" + str(path)).encode("utf-8")).hexdigest()
            if identity not in data["entries"]:
                data["entries"][identity] = {"kind": "discarded_plan", "status": "pending", "created_at": self.clock(), "targets": [target]}
            atomic_json_write(self.path, data)

    def collect_orphan_plans(self, days=7):
        """Age can discard unreferenced selection/search plans, never article runs or photos."""
        with self.lock:
            references = self._references()
            parent = self.root / "blog-runs"
            for path in parent.iterdir() if parent.is_dir() else ():
                if (path.name.startswith(_PLANS) and path.is_dir() and path.stat().st_mtime < self.clock() - days * 86400
                        and not any(_overlaps(path.resolve(), item) for item in references)):
                    self.enqueue_discarded_plan(str(path))

    def retry(self, max_targets=12):
        with self.lock:
            data = self._load()
            blocked = self._policy_paths(data)
            if not self.path.exists() and not data["entries"]:
                return
            atomic_json_write(self.path, data)  # Keep historical policy denials even if monitor logs later rotate.
            remaining = max_targets
            for entry in data["entries"].values():
                if entry["status"] != "pending" or remaining <= 0:
                    continue
                try:
                    run = self._verify_publication(entry) if entry["kind"] == "publication" else None
                    references = self._references(exclude_run=run)
                except (CleanupError, TopicHistoryError, OSError, ValueError, TypeError) as exc:
                    message = str(exc)[:500]
                    if entry.get("last_error") != message:
                        self.log("산출물 정리 보류 · " + message)
                    entry["last_error"] = message
                    atomic_json_write(self.path, data)
                    continue
                for target in entry["targets"]:
                    if target["status"] != "pending" or target["retry_at"] > self.clock() or remaining <= 0:
                        continue
                    try:
                        path = self._target(target["path"], run, plan_only=entry["kind"] == "discarded_plan")
                        if any(_overlaps(path, item) or (run and _overlaps(run, item)) for item in blocked):
                            target["status"] = "blocked"
                            target["last_error"] = "삭제 차단 정책에 따라 보존"
                        elif any(_overlaps(path, item) for item in references):
                            target["retry_at"] = self.clock() + 3600
                            target["last_error"] = "다른 회차가 참조하는 자료 보존"
                        elif not path.exists():
                            target["status"] = "done"
                        elif [path.stat().st_dev, path.stat().st_ino] != target["identity"]:
                            target["status"] = "blocked"
                            target["last_error"] = "대기열 등록 후 폴더가 교체되어 보존"
                        else:
                            remaining -= 1
                            target["attempts"] += 1
                            atomic_json_write(self.path, data)
                            shutil.rmtree(path)
                            target["status"] = "done"
                            self.log(f"발행·폐기 확인 산출물 정리 완료 · {path.name}")
                    except CleanupError as exc:
                        target["status"], target["last_error"] = "blocked", str(exc)[:500]
                    except OSError as exc:
                        # Sharing violations can clear; access denials are never retried through another path.
                        denied = isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {32, 33}
                        target["status"] = "blocked" if denied else "pending"
                        target["retry_at"] = self.clock() + 3600
                        target["last_error"] = str(exc)[:500]
                        self.log(f"산출물 정리 {'차단 · 보존' if denied else '대기 · 다음 회차 재시도'} · {target['path']}: {exc}")
                    atomic_json_write(self.path, data)
                states = {target["status"] for target in entry["targets"]}
                entry["status"] = "pending" if "pending" in states else "blocked" if "blocked" in states else "done"
                atomic_json_write(self.path, data)
