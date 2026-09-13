"""Recover a hash-verified partial manuscript for private Naver storage only.

Matches the Windows recovery contract, without importing its Tk UI.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from blog_workflow import WorkflowError, _json_hash

def recover_private_draft(app_dir, run_dir, topic, keywords):
    """Recover complete saved text for private storage without approving it."""
    if not run_dir:
        return None
    run = Path(run_dir).resolve()
    root = (Path(app_dir) / "blog-runs").resolve()
    if root not in run.parents or not run.is_dir() or run != Path(os.path.abspath(run_dir)):
        return None
    def valid_text(candidate):
        return (isinstance(candidate, dict) and isinstance(candidate.get("title"), str)
            and 1 <= len(candidate["title"].strip()) <= 100 and "\n" not in candidate["title"]
            and isinstance(candidate.get("paragraphs"), list) and 1 <= len(candidate["paragraphs"]) <= 8
            and all(isinstance(value, str) and value.strip() for value in candidate["paragraphs"])
            and not re.search(r'https?://', '\n'.join([candidate["title"], *candidate["paragraphs"]]), re.I))
    checkpoints = sorted(run.glob("stage-*.checkpoint.json"),
                         key=lambda path: path.stat().st_mtime, reverse=True)
    article, article_mtime = None, -1
    for checkpoint_path in checkpoints:
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            name = checkpoint.get("response_name")
            if not isinstance(name, str) or not name or Path(name).name != name:
                continue
            response_path = (run / f"{name}.json").resolve()
            if response_path.parent != run or not response_path.is_file():
                continue
            candidate = json.loads(response_path.read_text(encoding="utf-8"))
            if checkpoint.get("article_sha256") != _json_hash(candidate):
                continue
            if not valid_text(candidate):
                continue
            article = candidate
            article['draft_source_files'] = [checkpoint_path.name, response_path.name]
            article_mtime = max(checkpoint_path.stat().st_mtime, response_path.stat().st_mtime)
            break
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    # Later paired stage output can contain more work than an earlier
    # approved checkpoint. Choose the latest usable copy for private save.
    from blog_workflow import _parse_json
    for response_path in sorted(run.glob('stage-*.json'), key=lambda path: path.stat().st_mtime, reverse=True):
        raw_path = response_path.with_suffix('.response.txt')
        try:
            if (response_path.resolve().parent != run or raw_path.resolve().parent != run
                    or not raw_path.is_file() or response_path.stat().st_size > 2 * 1024 * 1024
                    or raw_path.stat().st_size > 2 * 1024 * 1024):
                continue
            modified = max(response_path.stat().st_mtime, raw_path.stat().st_mtime)
            if modified <= article_mtime:
                continue
            candidate = json.loads(response_path.read_text(encoding='utf-8'))
            raw = _parse_json(raw_path.read_text(encoding='utf-8'))
            if not valid_text(candidate) or _json_hash(candidate) != _json_hash(raw):
                continue
            article, article_mtime = candidate, modified
            article['draft_source_files'] = [response_path.name, raw_path.name]
            article['unapproved_private_recovery'] = True
        except (OSError, ValueError, TypeError, AttributeError, WorkflowError):
            continue
    if article is None:
        return None
    try:
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    images, seen = [], set()
    for item in [*(manifest.get("images") if isinstance(manifest.get("images"), list) else []),
                 *(manifest.get("image_candidates") if isinstance(manifest.get("image_candidates"), list) else [])]:
        if not isinstance(item, dict) or item.get("provider") == "google":
            continue
        key = (item.get("path"), item.get("sha256"), item.get("paragraph_index"))
        if not key[0] or key in seen or run not in Path(key[0]).resolve().parents:
            continue
        seen.add(key)
        images.append(copy.deepcopy(item))
    article.update({"topic": topic, "keywords": list(keywords), "run_dir": str(run),
                    "images": images[:16], "google_images": [], "ready_to_publish": False,
                    "quality_hold": True})
    article['draft_source_sha256'] = {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
        for name in article.get('draft_source_files', []) if (run / name).is_file()}
    article['draft_artifact_sha256'] = {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
        for name in ('request.json', 'manifest.json', 'editorial.pending.json') if (run / name).is_file()}
    from blog_quality import layout_article
    article, _ = layout_article(article)
    article['text'] = article['title'].strip() + '\n\n' + '\n\n'.join(article['paragraphs'])
    return article
