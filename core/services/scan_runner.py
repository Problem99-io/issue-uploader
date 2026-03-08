import threading
import json
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from django.db import close_old_connections
from django.utils import timezone

from core.models import IssueCandidate, ScanIssueLog, ScanStepLog, ScanTask
from core.services.agent_config import get_active_agent_config, get_global_settings
from core.services.github_client import (
    GitHubRateLimitError,
    GitHubServiceError,
    find_closing_pull_request_number,
    get_pull_request,
    list_issue_comments,
    list_pull_request_files,
    list_repository_issues,
)
from core.services.ollama_client import send_message
from core.services.problem99_client import Problem99ServiceError, upload_problem_direct


class ScanRunnerError(Exception):
    pass


class ScanRunnerStopped(ScanRunnerError):
    pass


_RUNNER_LOCK = threading.Lock()
_STOP_EVENTS: dict[int, threading.Event] = {}
_UPLOAD_CONFIDENCE_THRESHOLD = Decimal('0.85')
_BATCH_SIZE = 10  # issues per AI batch (context-fetch + AI)
_ISSUE_FETCH_CHUNK = 200  # issues to fetch before processing (2 pages of 100)
_AI_CONCURRENCY = 10
_GITHUB_IO_CONCURRENCY = 10
_LOG_FLUSH_INTERVAL = 10  # flush step logs every N entries
_RATE_LIMIT_WAIT_SECONDS = 61 * 60  # 61 minutes

_ALLOWED_PROGRAMMING_LANGUAGES = {
    'python', 'javascript', 'typescript', 'java', 'go', 'rust', 'c', 'cpp', 'csharp', 'php', 'ruby', 'kotlin',
    'swift', 'scala', 'elixir', 'erlang', 'dart', 'r', 'matlab', 'haskell', 'lua', 'perl', 'objective-c',
    'shell', 'bash', 'powershell', 'sql', 'html', 'css', 'scss', 'less', 'xml', 'yaml', 'toml', 'json',
    'dockerfile', 'docker compose', 'makefile', 'terraform', 'ansible',
}

_HUMAN_LANGUAGES = {
    'english', 'spanish', 'french', 'german', 'portuguese', 'italian', 'dutch', 'russian', 'chinese', 'japanese',
    'korean', 'arabic', 'hebrew', 'hindi', 'turkish', 'polish', 'ukrainian',
}


class _StepLogBuffer:
    """Buffers ScanStepLog entries and flushes to DB periodically."""

    def __init__(self, scan_task_id: int, flush_size: int = _LOG_FLUSH_INTERVAL):
        self._scan_task_id = scan_task_id
        self._flush_size = flush_size
        self._buffer: deque[dict] = deque()
        self._lock = threading.Lock()

    def log(
        self,
        phase: str,
        message: str,
        *,
        level: str = ScanStepLog.Level.INFO,
        issue_number: int | None = None,
        detail: str = '',
    ) -> None:
        entry = {
            'scan_task_id': self._scan_task_id,
            'phase': phase,
            'level': level,
            'issue_number': issue_number,
            'message': message,
            'detail': detail,
        }
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) >= self._flush_size:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        batch = list(self._buffer)
        self._buffer.clear()
        try:
            ScanStepLog.objects.bulk_create(
                [ScanStepLog(**entry) for entry in batch],
                ignore_conflicts=False,
            )
        except Exception:
            pass  # Non-critical; don't crash the scan


def _sanitize_text(value: str) -> str:
    text = (value or '').strip()
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]', text)
    text = re.sub(r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b', '[IP]', text)
    text = re.sub(r'https?://[^\s\'\"]+', '[URL]', text)
    text = re.sub(r'/(?:home|Users|usr|var|tmp|etc|opt)/[^\s\'\"]+', '[PATH]', text)
    text = re.sub(r'[A-Z]:\\[^\s\'\"]+', '[PATH]', text)
    text = re.sub(r'\b(?:sk|pk|api|key|token|secret|password)[_-]?[A-Za-z0-9]{20,}\b', '[TOKEN]', text, flags=re.IGNORECASE)
    return text


def _sanitize_github_references(value: str) -> str:
    text = (value or '').strip()
    text = re.sub(r'\b(issue\s*)?#\d{1,6}\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'https?://github\.com/[^\s)\]>]+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+\b(?=\s|$|[,.)>\]])', '', text)
    text = re.sub(r'\b(PR|pull\s*request)\s*#?\d+\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b[a-f0-9]{7,40}\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\[\s*\]', '', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\.\s*\.', '.', text)
    return text.strip()


def _passes_problem99_quality_gate(payload: dict) -> tuple[bool, str]:
    error_message = (payload.get('error_message') or '').strip()
    solution_code = (payload.get('solution_code') or '').strip()
    language = (payload.get('language') or '').strip().lower()
    explanation = (payload.get('explanation') or '').strip()

    if len(error_message) < 20:
        return False, 'error_message_too_short'
    if len(solution_code) < 60:
        return False, 'solution_code_too_short'
    if len(explanation) < 40:
        return False, 'explanation_too_short'
    if language in {'', 'unknown', 'n/a', 'none'}:
        return False, 'language_missing'
    if language in _HUMAN_LANGUAGES:
        return False, 'language_is_human_language'
    if language not in _ALLOWED_PROGRAMMING_LANGUAGES:
        return False, 'language_not_programming_language'

    content = f"{error_message} {explanation}".lower()
    technical_markers = (
        'error',
        'exception',
        'fail',
        'bug',
        'issue',
        'undefined',
        'null',
        'crash',
        'invalid',
        'missing',
    )
    if not any(marker in content for marker in technical_markers):
        return False, 'not_technical_enough'

    return True, ''


def _passes_verbose_candidate_gate(payload: dict) -> tuple[bool, str]:
    solution_code = (payload.get('solution_code') or '').strip()
    explanation = (payload.get('explanation') or '').strip()
    error_message = (payload.get('error_message') or '').strip()
    language = (payload.get('language') or '').strip().lower()

    if len(error_message) < 25:
        return False, 'candidate_error_too_short'
    if len(solution_code) < 40:
        return False, 'candidate_solution_too_short'
    if len(explanation) < 80:
        return False, 'candidate_explanation_too_short'
    if language in _HUMAN_LANGUAGES:
        return False, 'candidate_language_is_human_language'
    if language not in _ALLOWED_PROGRAMMING_LANGUAGES:
        return False, 'candidate_language_not_programming_language'
    return True, ''


def _format_solution_payload(data: dict) -> str:
    raw_tags = data.get('tags') or []
    if isinstance(raw_tags, str):
        tags = [segment.strip() for segment in raw_tags.split(',') if segment.strip()]
    else:
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    tags = [_sanitize_text(tag) for tag in tags][:8]

    payload = {
        'language': _sanitize_text(str(data.get('language') or 'unknown')),
        'framework': _sanitize_text(str(data.get('framework') or 'unknown')),
        'error_message': _sanitize_text(
            str(data.get('error_message') or data.get('error_title_or_code') or data.get('error_code') or 'unknown')
        ),
        'solution_code': _sanitize_text(str(data.get('solution_code') or '')),
        'explanation': _sanitize_text(str(data.get('explanation') or '')),
        'tags': tags,
    }
    return json.dumps(payload, indent=2)


def _extract_json_object(text: str) -> dict | None:
    payload = (text or '').strip()
    if not payload:
        return None

    fenced_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', payload, flags=re.DOTALL)
    if fenced_match:
        payload = fenced_match.group(1).strip()
    else:
        object_match = re.search(r'\{.*\}', payload, flags=re.DOTALL)
        if object_match:
            payload = object_match.group(0).strip()

    try:
        parsed = json.loads(payload)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _score_issue_with_ai(
    issue: dict,
    comments: list[dict],
    pull_request: dict | None,
    pull_files: list[dict],
    ollama_base_url: str,
    model: str,
    temperature: float,
) -> tuple[bool, Decimal, str, bool, str]:
    if not ollama_base_url or not model:
        return False, Decimal('0.00'), '', False, 'missing_model_or_base_url'

    labels = ', '.join([label for label in issue.get('labels', []) if label]) or 'none'
    body = (issue.get('body') or '').strip()
    if len(body) > 2000:
        body = f"{body[:2000]}..."

    comments_text = []
    for idx, comment in enumerate(comments[:20], start=1):
        raw = (comment or {}).get('body', '').strip()
        if not raw:
            continue
        if len(raw) > 500:
            raw = f"{raw[:500]}..."
        comments_text.append(f"Comment {idx}: {raw}")
    comments_block = '\\n'.join(comments_text) if comments_text else 'No comments available.'

    pr_title = ''
    pr_body = ''
    pr_url = ''
    if pull_request:
        pr_title = (pull_request.get('title') or '').strip()
        pr_body = (pull_request.get('body') or '').strip()
        pr_url = (pull_request.get('html_url') or '').strip()
        if len(pr_body) > 2000:
            pr_body = f"{pr_body[:2000]}..."

    pr_files_text = []
    patch_budget = 8000
    for file_item in pull_files[:20]:
        filename = (file_item.get('filename') or '').strip()
        patch = (file_item.get('patch') or '').strip()
        if not filename or not patch:
            continue
        if len(patch) > 1200:
            patch = f"{patch[:1200]}..."
        segment = f"File: {filename}\\n```diff\\n{patch}\\n```"
        if patch_budget - len(segment) <= 0:
            break
        pr_files_text.append(segment)
        patch_budget -= len(segment)
    pr_files_block = '\\n\\n'.join(pr_files_text) if pr_files_text else 'No PR patch data available.'

    prompt = (
        'You are an issue triage assistant.\\n'
        'Step 1: Check whether this issue has a clear error/problem and a clear solution in the issue body or comments '
        '(including fixes explained in comments, commits, or PR references).\\n'
        'Step 2: If yes, produce structured, reusable output without names or personal information.\\n'
        'CRITICAL: Use only facts present in issue body, comments, PR body, and PR diff data provided below.\\n'
        'Do not invent solutions. If the fix is not explicitly present in comments/PR, set include=false.\\n'
        'language must be the PROGRAMMING language of the fix (e.g. python, javascript, typescript, go, rust), never human language names like English.\\n'
        'The solution_code must be a concrete snippet (preferably 3-20 lines) showing the actual fix, not a one-liner summary.\\n'
        'The explanation must be detailed (at least 3 sentences): root cause, triggering conditions, and why fix works.\\n'
        'Extract ONLY factual information present in the issue and comments.\\n'
        'Do NOT invent details or speculate.\\n'
        'Do NOT include issue numbers, PR numbers, commit SHAs, repository names, contributor names, or GitHub URLs.\\n'
        'The output must be generic and reusable for similar problems.\\n'
        'If the thread is unclear, incomplete, or lacks a concrete fix, set include=false.\\n'
        'Return ONLY valid JSON. No markdown, no prose, no code fences.\\n'
        'Use exactly this schema and key names:\\n'
        '{\\n'
        '  "include": true|false,\\n'
        '  "confidence": 0.0..1.0,\\n'
        '  "language": "string",\\n'
        '  "framework": "string",\\n'
        '  "error_message": "string",\\n'
        '  "solution_code": "string",\\n'
        '  "explanation": "string",\\n'
        '  "tags": ["tag1", "tag2"]\\n'
        '}\\n'
        'Do not include user names, emails, profile handles, private URLs, or personal identifiers.\\n'
        'If there is no clear error + solution, set include=false and still return the full JSON schema with empty strings and empty tags.\\n\\n'
        f"Title: {issue.get('title', '')}\\n"
        f"State: {issue.get('state', 'open')}\\n"
        f"Labels: {labels}\\n"
        f"Body: {body}\\n\\n"
        f"Comments:\\n{comments_block}\\n\\n"
        f"PR Title: {pr_title}\\n"
        f"PR URL: {pr_url}\\n"
        f"PR Body: {pr_body or 'No PR body available.'}\\n\\n"
        f"PR Patch Excerpts:\\n{pr_files_block}"
    )

    try:
        reply = send_message(
            base_url=ollama_base_url,
            model=model,
            message=prompt,
            temperature=temperature,
        )
    except Exception:
        return False, Decimal('0.00'), '', False, 'chat_error_or_timeout'

    data = _extract_json_object(reply)
    if not data:
        return False, Decimal('0.00'), '', False, 'invalid_json_reply'

    try:
        include = bool(data.get('include'))
        confidence = Decimal(str(data.get('confidence', '0.00')))
        error_message = _sanitize_github_references(
            _sanitize_text(str(data.get('error_message') or data.get('error_title_or_code') or data.get('error_code') or ''))
        )
        solution_code = _sanitize_github_references(_sanitize_text(str(data.get('solution_code') or '')))
        framework = _sanitize_github_references(_sanitize_text(str(data.get('framework') or ''))).lower()
        language = _sanitize_github_references(_sanitize_text(str(data.get('language') or ''))).lower()
        explanation = _sanitize_github_references(_sanitize_text(str(data.get('explanation') or '')))
        tags = data.get('tags') or []
    except Exception:
        return False, Decimal('0.00'), '', False, 'invalid_confidence_reply'

    if confidence < Decimal('0.00'):
        confidence = Decimal('0.00')
    if confidence > Decimal('0.99'):
        confidence = Decimal('0.99')

    has_required_fields = all([error_message, solution_code, language, explanation])
    if not has_required_fields:
        include = False

    structured = _format_solution_payload(
        {
            'error_message': error_message,
            'solution_code': solution_code,
            'framework': framework,
            'language': language,
            'explanation': explanation,
            'tags': tags,
        }
    )
    return include, confidence.quantize(Decimal('0.01')), structured, True, ''


def _append_error_note(scan_task: ScanTask, message: str) -> None:
    note = f"[{timezone.now().isoformat(timespec='seconds')}] {message}"
    if scan_task.notes.strip():
        scan_task.notes = f"{scan_task.notes}\n\n{note}"
    else:
        scan_task.notes = note


def _get_stop_event(scan_task_id: int) -> threading.Event:
    with _RUNNER_LOCK:
        event = _STOP_EVENTS.get(scan_task_id)
        if event is None:
            event = threading.Event()
            _STOP_EVENTS[scan_task_id] = event
        return event


def request_stop_scan_task(scan_task_id: int) -> None:
    _get_stop_event(scan_task_id).set()


def _clear_stop_event(scan_task_id: int) -> None:
    with _RUNNER_LOCK:
        _STOP_EVENTS.pop(scan_task_id, None)


def _is_stop_requested(scan_task_id: int, stop_event: threading.Event) -> bool:
    if stop_event.is_set():
        return True
    return ScanTask.objects.filter(pk=scan_task_id, cancel_requested=True).exists()


def _wait_for_rate_limit(
    scan_task: ScanTask,
    scan_task_id: int,
    stop_event: threading.Event,
    step_log: '_StepLogBuffer',
    wait_seconds: int = _RATE_LIMIT_WAIT_SECONDS,
) -> None:
    """Block until rate limit wait expires or the task is stopped."""
    resume_at = timezone.now() + timezone.timedelta(seconds=wait_seconds)
    wait_minutes = wait_seconds // 60
    scan_task.current_phase = f'Rate limited — waiting {wait_minutes}m (resumes ~{resume_at.strftime("%H:%M UTC")})'
    scan_task.save(update_fields=['current_phase'])

    step_log.log(
        ScanStepLog.Phase.FETCH_COMMENTS,
        f"GitHub rate limit hit. Pausing for {wait_minutes} minutes until ~{resume_at.strftime('%H:%M UTC')}.",
        level=ScanStepLog.Level.WARNING,
    )
    step_log.flush()

    # Sleep in 30-second increments so we can respond to stop requests
    elapsed = 0
    while elapsed < wait_seconds:
        if _is_stop_requested(scan_task_id, stop_event):
            raise ScanRunnerStopped('Stopped by user during rate-limit wait.')
        chunk = min(30, wait_seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk

    step_log.log(
        ScanStepLog.Phase.FETCH_COMMENTS,
        'Rate limit wait finished. Resuming scan.',
        level=ScanStepLog.Level.INFO,
    )
    step_log.flush()


def _fetch_issue_context(
    issue: dict,
    full_name: str,
    github_api_key: str,
    step_log: _StepLogBuffer,
) -> dict:
    """Fetch comments, timeline PR, and PR files for one issue. Runs in GitHub I/O pool."""
    issue_number = issue.get('number')

    comments = []
    try:
        step_log.log(
            ScanStepLog.Phase.FETCH_COMMENTS,
            f"Fetching comments for issue #{issue_number}",
            issue_number=issue_number,
            level=ScanStepLog.Level.DEBUG,
        )
        comments = list_issue_comments(
            api_key=github_api_key,
            full_name=full_name,
            issue_number=issue_number,
            per_page=20,
            page=1,
        )
        step_log.log(
            ScanStepLog.Phase.FETCH_COMMENTS,
            f"Fetched {len(comments)} comment(s) for issue #{issue_number}",
            issue_number=issue_number,
            level=ScanStepLog.Level.DEBUG,
        )
    except GitHubRateLimitError:
        raise
    except Exception as exc:
        step_log.log(
            ScanStepLog.Phase.FETCH_COMMENTS,
            f"Failed to fetch comments for issue #{issue_number}: {exc}",
            issue_number=issue_number,
            level=ScanStepLog.Level.WARNING,
        )
        comments = []

    closing_pr = None
    pr_files = []
    try:
        step_log.log(
            ScanStepLog.Phase.FETCH_TIMELINE,
            f"Checking for closing PR on issue #{issue_number}",
            issue_number=issue_number,
            level=ScanStepLog.Level.DEBUG,
        )
        closing_pr_number = find_closing_pull_request_number(
            api_key=github_api_key,
            full_name=full_name,
            issue_number=issue_number,
        )
        if closing_pr_number:
            step_log.log(
                ScanStepLog.Phase.FETCH_PR,
                f"Found closing PR #{closing_pr_number} for issue #{issue_number}",
                issue_number=issue_number,
            )
            closing_pr = get_pull_request(
                api_key=github_api_key,
                full_name=full_name,
                pull_number=closing_pr_number,
            )
            step_log.log(
                ScanStepLog.Phase.FETCH_PR_FILES,
                f"Fetching PR #{closing_pr_number} files",
                issue_number=issue_number,
                level=ScanStepLog.Level.DEBUG,
            )
            pr_files = list_pull_request_files(
                api_key=github_api_key,
                full_name=full_name,
                pull_number=closing_pr_number,
                per_page=100,
                page=1,
            )
            step_log.log(
                ScanStepLog.Phase.FETCH_PR_FILES,
                f"Fetched {len(pr_files)} file(s) from PR #{closing_pr_number}",
                issue_number=issue_number,
                level=ScanStepLog.Level.DEBUG,
            )
        else:
            step_log.log(
                ScanStepLog.Phase.FETCH_TIMELINE,
                f"No closing PR found for issue #{issue_number}",
                issue_number=issue_number,
                level=ScanStepLog.Level.DEBUG,
            )
    except GitHubRateLimitError:
        raise
    except Exception as exc:
        step_log.log(
            ScanStepLog.Phase.FETCH_PR,
            f"Failed PR lookup for issue #{issue_number}: {exc}",
            issue_number=issue_number,
            level=ScanStepLog.Level.WARNING,
        )
        closing_pr = None
        pr_files = []

    return {
        'issue': issue,
        'comments': comments,
        'closing_pr': closing_pr,
        'pr_files': pr_files,
    }


def _analyze_issue_with_ai(
    context: dict,
    ollama_base_url: str,
    analysis_model: str,
    temperature: float,
    step_log: _StepLogBuffer,
) -> dict:
    """Run AI analysis on a pre-fetched issue context. Runs in AI pool."""
    issue = context['issue']
    comments = context['comments']
    closing_pr = context['closing_pr']
    pr_files = context['pr_files']

    issue_number = issue.get('number')
    title = (issue.get('title') or '').strip()
    issue_url = issue.get('html_url') or ''

    if not issue_number or not title or not issue_url:
        return {'skip': True, 'reason': 'missing_issue_fields'}

    step_log.log(
        ScanStepLog.Phase.AI_RUNNING,
        f"Running AI analysis on issue #{issue_number}: {title[:80]}",
        issue_number=issue_number,
    )

    include, confidence, summary, ai_used, ai_error = _score_issue_with_ai(
        issue=issue,
        comments=comments,
        pull_request=closing_pr,
        pull_files=pr_files,
        ollama_base_url=ollama_base_url,
        model=analysis_model,
        temperature=temperature,
    )

    if ai_error:
        step_log.log(
            ScanStepLog.Phase.AI_ERROR,
            f"AI error for issue #{issue_number}: {ai_error}",
            issue_number=issue_number,
            level=ScanStepLog.Level.ERROR,
        )
    else:
        step_log.log(
            ScanStepLog.Phase.AI_DONE,
            f"AI analysis complete for issue #{issue_number}: include={include}, confidence={confidence}",
            issue_number=issue_number,
        )

    return {
        'skip': False,
        'issue_number': issue_number,
        'title': title,
        'issue_url': issue_url,
        'include': include,
        'confidence': confidence,
        'summary': summary,
        'ai_used': ai_used,
        'ai_error': ai_error,
    }


def _update_progress_counters(scan_task: ScanTask, **kwargs) -> None:
    """Update only the specified counter fields on the scan task."""
    update_fields = []
    for field, value in kwargs.items():
        setattr(scan_task, field, value)
        update_fields.append(field)
    if update_fields:
        scan_task.save(update_fields=update_fields)


def _fetch_batch_contexts_with_rate_limit_retry(
    batch: list[dict],
    full_name: str,
    github_api_key: str,
    step_log: _StepLogBuffer,
    scan_task: ScanTask,
    scan_task_id: int,
    stop_event: threading.Event,
    batch_num: int,
    total_batches: int,
) -> list[dict]:
    """Fetch issue contexts for a batch, retrying the entire batch if rate-limited."""
    while True:
        rate_limited = False
        batch_contexts: list[dict] = []

        with ThreadPoolExecutor(max_workers=_GITHUB_IO_CONCURRENCY) as io_pool:
            io_futures = {
                io_pool.submit(
                    _fetch_issue_context, issue, full_name, github_api_key, step_log
                ): issue
                for issue in batch
            }
            for future in as_completed(io_futures):
                if _is_stop_requested(scan_task_id, stop_event):
                    for pending in io_futures:
                        pending.cancel()
                    raise ScanRunnerStopped('Stopped by user.')
                try:
                    ctx = future.result()
                    batch_contexts.append(ctx)
                except GitHubRateLimitError:
                    rate_limited = True
                    # Cancel remaining futures — we'll retry the whole batch
                    for pending in io_futures:
                        pending.cancel()
                    break
                except Exception:
                    failed_issue = io_futures[future]
                    step_log.log(
                        ScanStepLog.Phase.FETCH_COMMENTS,
                        f"Context prefetch failed for issue #{failed_issue.get('number', '?')}",
                        issue_number=failed_issue.get('number'),
                        level=ScanStepLog.Level.ERROR,
                    )

        if not rate_limited:
            return batch_contexts

        # Rate limited — wait and retry the entire batch
        _wait_for_rate_limit(scan_task, scan_task_id, stop_event, step_log)
        scan_task.current_phase = f'Fetching context (batch {batch_num}/{total_batches}) [retry after rate limit]'
        scan_task.save(update_fields=['current_phase'])
        step_log.log(
            ScanStepLog.Phase.FETCH_COMMENTS,
            f"Retrying batch {batch_num}/{total_batches} after rate limit wait",
        )


def run_scan_task(scan_task_id: int) -> None:
    close_old_connections()
    stop_event = _get_stop_event(scan_task_id)
    stop_event.clear()
    try:
        scan_task = ScanTask.objects.select_related('repository').get(pk=scan_task_id)
    except ScanTask.DoesNotExist:
        return

    if scan_task.status == ScanTask.Status.RUNNING:
        return

    scan_task.status = ScanTask.Status.RUNNING
    scan_task.started_at = timezone.now()
    scan_task.finished_at = None
    scan_task.cancel_requested = False
    scan_task.current_phase = 'Initialising'
    scan_task.current_issue = None
    scan_task.total_issues = 0
    scan_task.processed_issues = 0
    scan_task.matched_issues = 0
    scan_task.uploaded_issues = 0
    scan_task.skipped_issues = 0
    scan_task.error_issues = 0
    scan_task.save(
        update_fields=[
            'status', 'started_at', 'finished_at', 'cancel_requested',
            'current_phase', 'current_issue',
            'total_issues', 'processed_issues', 'matched_issues',
            'uploaded_issues', 'skipped_issues', 'error_issues',
        ]
    )
    IssueCandidate.objects.filter(scan_task=scan_task).delete()
    ScanIssueLog.objects.filter(scan_task=scan_task).delete()
    ScanStepLog.objects.filter(scan_task=scan_task).delete()

    step_log = _StepLogBuffer(scan_task_id)

    try:
        active_config = get_active_agent_config()
        global_settings = get_global_settings()
        if not active_config:
            raise ScanRunnerError('No active agent config. Configure one before running scans.')
        github_api_key = (global_settings.github_api_key or '').strip()
        problem99_api_key = (global_settings.problem99_api_key or '').strip()
        ollama_base_url = (global_settings.ollama_base_url or '').strip()
        if not github_api_key:
            raise ScanRunnerError('Global settings has no GitHub API key.')
        if not ollama_base_url:
            raise ScanRunnerError('Global Ollama URL is not configured.')

        analysis_model = active_config.llm_model
        temperature = float(active_config.temperature)
        full_name = scan_task.repository.full_name

        step_log.log(
            ScanStepLog.Phase.SCAN_START,
            f"Scan started for {full_name} using model {analysis_model}",
        )

        # Counters (accumulated across all chunks)
        matched_count = 0
        processed_count = 0
        uploaded_count = 0
        skipped_count = 0
        error_count = 0
        ai_attempted = 0
        ai_success = 0
        ai_failures: dict[str, int] = {}
        total_issues_seen = 0
        batch_num = 0
        per_page = 100
        page = 1
        more_pages = True

        # Streaming loop: fetch _ISSUE_FETCH_CHUNK issues, then process, then fetch more
        while more_pages:
            # --- Fetch up to _ISSUE_FETCH_CHUNK issues (multiple pages) ---
            scan_task.current_phase = f'Fetching issues (from page {page})'
            scan_task.save(update_fields=['current_phase'])
            chunk_issues: list[dict] = []
            pages_fetched = 0

            while len(chunk_issues) < _ISSUE_FETCH_CHUNK:
                if _is_stop_requested(scan_task_id, stop_event):
                    raise ScanRunnerStopped('Stopped by user.')
                step_log.log(
                    ScanStepLog.Phase.FETCH_ISSUES_PAGE,
                    f"Fetching closed issues page {page} (per_page={per_page})",
                )
                while True:
                    try:
                        page_items = list_repository_issues(
                            api_key=github_api_key,
                            full_name=full_name,
                            state='closed',
                            per_page=per_page,
                            page=page,
                        )
                        break  # success — exit retry loop
                    except GitHubRateLimitError:
                        _wait_for_rate_limit(scan_task, scan_task_id, stop_event, step_log)
                        scan_task.current_phase = f'Fetching issues (from page {page})'
                        scan_task.save(update_fields=['current_phase'])
                if not page_items:
                    more_pages = False
                    break
                filtered = [item for item in page_items if not item.get('is_pull_request')]
                chunk_issues.extend(filtered)
                step_log.log(
                    ScanStepLog.Phase.FETCH_ISSUES_PAGE,
                    f"Page {page}: got {len(page_items)} items, {len(filtered)} issues (chunk so far: {len(chunk_issues)})",
                )
                pages_fetched += 1
                if len(page_items) < per_page:
                    more_pages = False
                    break
                page += 1

            if not chunk_issues:
                break

            total_issues_seen += len(chunk_issues)
            scan_task.total_issues = total_issues_seen
            scan_task.save(update_fields=['total_issues'])
            step_log.log(
                ScanStepLog.Phase.FETCH_ISSUES_DONE,
                f"Fetched chunk of {len(chunk_issues)} issues ({pages_fetched} page(s)); {total_issues_seen} total so far",
            )
            step_log.flush()

            # --- Process this chunk in AI batches of _BATCH_SIZE ---
            chunk_batches = (len(chunk_issues) + _BATCH_SIZE - 1) // _BATCH_SIZE
            step_log.log(
                ScanStepLog.Phase.AI_QUEUED,
                f"Processing chunk of {len(chunk_issues)} issues in {chunk_batches} batch(es) of {_BATCH_SIZE}",
            )
            step_log.flush()

            for batch_idx in range(0, len(chunk_issues), _BATCH_SIZE):
                if _is_stop_requested(scan_task_id, stop_event):
                    raise ScanRunnerStopped('Stopped by user.')

                batch = chunk_issues[batch_idx:batch_idx + _BATCH_SIZE]
                batch_num += 1

                # Step A: Fetch context for this batch in parallel (with rate-limit retry)
                scan_task.current_phase = f'Fetching context (batch {batch_num})'
                scan_task.save(update_fields=['current_phase'])
                step_log.log(
                    ScanStepLog.Phase.FETCH_COMMENTS,
                    f"Batch {batch_num}: fetching context for {len(batch)} issues",
                )

                batch_contexts: list[dict] = _fetch_batch_contexts_with_rate_limit_retry(
                    batch=batch,
                    full_name=full_name,
                    github_api_key=github_api_key,
                    step_log=step_log,
                    scan_task=scan_task,
                    scan_task_id=scan_task_id,
                    stop_event=stop_event,
                    batch_num=batch_num,
                    total_batches=batch_num,  # not known upfront; use current as placeholder
                )

                if _is_stop_requested(scan_task_id, stop_event):
                    raise ScanRunnerStopped('Stopped by user.')

                # Step B: Run AI analysis on this batch in parallel
                scan_task.current_phase = f'AI analysis (batch {batch_num})'
                scan_task.save(update_fields=['current_phase'])
                step_log.log(
                    ScanStepLog.Phase.AI_RUNNING,
                    f"Batch {batch_num}: running AI on {len(batch_contexts)} issues",
                )

                with ThreadPoolExecutor(max_workers=_AI_CONCURRENCY) as ai_pool:
                    ai_futures = {
                        ai_pool.submit(
                            _analyze_issue_with_ai,
                            ctx,
                            ollama_base_url,
                            analysis_model,
                            temperature,
                            step_log,
                        ): ctx
                        for ctx in batch_contexts
                    }

                    for future in as_completed(ai_futures):
                        if _is_stop_requested(scan_task_id, stop_event):
                            for pending in ai_futures:
                                pending.cancel()
                            raise ScanRunnerStopped('Stopped by user.')

                        processed_count += 1

                        try:
                            result = future.result()
                        except Exception:
                            ai_attempted += 1
                            error_count += 1
                            ai_failures['worker_error'] = ai_failures.get('worker_error', 0) + 1
                            _update_progress_counters(
                                scan_task,
                                processed_issues=processed_count,
                                error_issues=error_count,
                            )
                            continue

                        if result.get('skip'):
                            skipped_count += 1
                            _update_progress_counters(
                                scan_task,
                                processed_issues=processed_count,
                                skipped_issues=skipped_count,
                            )
                            continue

                        issue_number = result['issue_number']
                        title = result['title']
                        issue_url = result['issue_url']
                        include = result['include']
                        confidence = result['confidence']
                        summary = result['summary']
                        ai_used = result['ai_used']
                        ai_error = result['ai_error']

                        scan_task.current_issue = issue_number
                        scan_task.save(update_fields=['current_issue'])

                        ai_attempted += 1
                        if ai_used:
                            ai_success += 1
                        elif ai_error:
                            error_count += 1
                            ai_failures[ai_error] = ai_failures.get(ai_error, 0) + 1
                            ScanIssueLog.objects.update_or_create(
                                scan_task=scan_task,
                                issue_number=issue_number,
                                defaults={
                                    'title': title,
                                    'issue_url': issue_url,
                                    'decision': ScanIssueLog.Decision.ERROR,
                                    'confidence_score': Decimal('0.00'),
                                    'reason': f'AI error: {ai_error}',
                                },
                            )
                            _update_progress_counters(
                                scan_task,
                                processed_issues=processed_count,
                                error_issues=error_count,
                            )
                            continue

                        if not include:
                            skipped_count += 1
                            ScanIssueLog.objects.update_or_create(
                                scan_task=scan_task,
                                issue_number=issue_number,
                                defaults={
                                    'title': title,
                                    'issue_url': issue_url,
                                    'decision': ScanIssueLog.Decision.SKIPPED,
                                    'confidence_score': confidence,
                                    'reason': summary,
                                },
                            )
                            step_log.log(
                                ScanStepLog.Phase.ISSUE_DONE,
                                f"Issue #{issue_number} skipped (include=false, confidence={confidence})",
                                issue_number=issue_number,
                                level=ScanStepLog.Level.DEBUG,
                            )
                            _update_progress_counters(
                                scan_task,
                                processed_issues=processed_count,
                                skipped_issues=skipped_count,
                            )
                            continue

                        # Quality gate check
                        candidate_payload = _extract_json_object(summary) or {}
                        candidate_ok, candidate_reason = _passes_verbose_candidate_gate(candidate_payload)

                        step_log.log(
                            ScanStepLog.Phase.QUALITY_GATE,
                            f"Issue #{issue_number} quality gate: {'PASS' if candidate_ok else 'FAIL'} ({candidate_reason or 'ok'})",
                            issue_number=issue_number,
                            level=ScanStepLog.Level.INFO if candidate_ok else ScanStepLog.Level.DEBUG,
                        )

                        if not candidate_ok:
                            skipped_count += 1
                            ScanIssueLog.objects.update_or_create(
                                scan_task=scan_task,
                                issue_number=issue_number,
                                defaults={
                                    'title': title,
                                    'issue_url': issue_url,
                                    'decision': ScanIssueLog.Decision.SKIPPED,
                                    'confidence_score': confidence,
                                    'reason': f'Insufficient detail: {candidate_reason}',
                                },
                            )
                            _update_progress_counters(
                                scan_task,
                                processed_issues=processed_count,
                                skipped_issues=skipped_count,
                            )
                            continue

                        IssueCandidate.objects.update_or_create(
                            scan_task=scan_task,
                            issue_number=issue_number,
                            defaults={
                                'title': title,
                                'issue_url': issue_url,
                                'state': 'closed',
                                'confidence_score': confidence,
                                'resolution_summary': summary,
                                'resolution_status': IssueCandidate.ResolutionStatus.NEW,
                            },
                        )
                        matched_count += 1
                        ScanIssueLog.objects.update_or_create(
                            scan_task=scan_task,
                            issue_number=issue_number,
                            defaults={
                                'title': title,
                                'issue_url': issue_url,
                                'decision': ScanIssueLog.Decision.INCLUDED,
                                'confidence_score': confidence,
                                'reason': summary,
                            },
                        )

                        step_log.log(
                            ScanStepLog.Phase.ISSUE_DONE,
                            f"Issue #{issue_number} INCLUDED (confidence={confidence})",
                            issue_number=issue_number,
                        )

                        # Problem99 upload -- immediately after AI confirms the candidate
                        if confidence >= _UPLOAD_CONFIDENCE_THRESHOLD and problem99_api_key:
                            payload = candidate_payload or _extract_json_object(summary) or {}
                            if payload:
                                is_valid, quality_reason = _passes_problem99_quality_gate(payload)
                                if not is_valid:
                                    step_log.log(
                                        ScanStepLog.Phase.UPLOAD_SKIP,
                                        f"Problem99 upload skipped for issue #{issue_number}: {quality_reason}",
                                        issue_number=issue_number,
                                        level=ScanStepLog.Level.DEBUG,
                                    )
                                    _append_error_note(
                                        scan_task,
                                        f'Problem99 upload skipped for issue #{issue_number}: {quality_reason}',
                                    )
                                else:
                                    try:
                                        step_log.log(
                                            ScanStepLog.Phase.UPLOAD_START,
                                            f"Uploading issue #{issue_number} to Problem99",
                                            issue_number=issue_number,
                                        )
                                        direct_payload = {
                                            'error_message': payload.get('error_message', ''),
                                            'solution_code': payload.get('solution_code', ''),
                                            'language': payload.get('language', ''),
                                            'explanation': payload.get('explanation', ''),
                                        }
                                        framework = (payload.get('framework') or '').strip()
                                        if framework:
                                            direct_payload['framework'] = framework

                                        upload_problem_direct(
                                            api_key=problem99_api_key,
                                            payload=direct_payload,
                                        )
                                        uploaded_count += 1
                                        step_log.log(
                                            ScanStepLog.Phase.UPLOAD_OK,
                                            f"Successfully uploaded issue #{issue_number} to Problem99",
                                            issue_number=issue_number,
                                        )
                                    except Problem99ServiceError as exc:
                                        step_log.log(
                                            ScanStepLog.Phase.UPLOAD_FAIL,
                                            f"Problem99 upload failed for issue #{issue_number}: {exc}",
                                            issue_number=issue_number,
                                            level=ScanStepLog.Level.ERROR,
                                        )
                                        _append_error_note(scan_task, f'Problem99 upload failed for issue #{issue_number}: {exc}')

                        _update_progress_counters(
                            scan_task,
                            processed_issues=processed_count,
                            matched_issues=matched_count,
                            uploaded_issues=uploaded_count,
                        )

                # End of batch -- flush logs
                step_log.log(
                    ScanStepLog.Phase.ISSUE_DONE,
                    f"Batch {batch_num} complete: {processed_count}/{total_issues_seen} processed so far",
                    level=ScanStepLog.Level.DEBUG,
                )
                step_log.flush()

        if _is_stop_requested(scan_task_id, stop_event):
            raise ScanRunnerStopped('Stopped by user.')

        scan_task.matched_issues = matched_count
        scan_task.processed_issues = processed_count
        scan_task.uploaded_issues = uploaded_count
        scan_task.skipped_issues = skipped_count
        scan_task.error_issues = error_count
        scan_task.status = ScanTask.Status.COMPLETED
        scan_task.finished_at = timezone.now()
        scan_task.cancel_requested = False
        scan_task.current_phase = ''
        scan_task.current_issue = None
        if ai_attempted:
            failure_parts = [f'{key}={value}' for key, value in sorted(ai_failures.items())]
            ai_note = f'AI triage attempts={ai_attempted}, success={ai_success}'
            if failure_parts:
                ai_note = f"{ai_note}, fallback_reasons=({', '.join(failure_parts)})"
            _append_error_note(scan_task, ai_note)
        scan_task.save(update_fields=[
            'matched_issues', 'processed_issues', 'uploaded_issues',
            'skipped_issues', 'error_issues',
            'status', 'finished_at', 'cancel_requested',
            'current_phase', 'current_issue', 'notes',
        ])
        step_log.log(
            ScanStepLog.Phase.SCAN_DONE,
            f"Scan completed: {matched_count} matched, {uploaded_count} uploaded, {skipped_count} skipped, {error_count} errors out of {total_issues_seen} issues",
        )
    except ScanRunnerStopped as exc:
        _append_error_note(scan_task, str(exc))
        scan_task.status = ScanTask.Status.STOPPED
        scan_task.finished_at = timezone.now()
        scan_task.cancel_requested = False
        scan_task.current_phase = ''
        scan_task.current_issue = None
        scan_task.save(update_fields=['status', 'finished_at', 'notes', 'cancel_requested', 'current_phase', 'current_issue'])
        step_log.log(
            ScanStepLog.Phase.SCAN_STOPPED,
            f"Scan stopped: {exc}",
            level=ScanStepLog.Level.WARNING,
        )
    except (GitHubServiceError, ScanRunnerError) as exc:
        _append_error_note(scan_task, f'Scan failed: {exc}')
        scan_task.status = ScanTask.Status.FAILED
        scan_task.finished_at = timezone.now()
        scan_task.current_phase = ''
        scan_task.current_issue = None
        scan_task.save(update_fields=['status', 'finished_at', 'notes', 'current_phase', 'current_issue'])
        step_log.log(
            ScanStepLog.Phase.SCAN_ERROR,
            f"Scan failed: {exc}",
            level=ScanStepLog.Level.ERROR,
        )
    except Exception as exc:  # pragma: no cover - safeguard
        _append_error_note(scan_task, f'Unexpected scan failure: {exc}')
        scan_task.status = ScanTask.Status.FAILED
        scan_task.finished_at = timezone.now()
        scan_task.current_phase = ''
        scan_task.current_issue = None
        scan_task.save(update_fields=['status', 'finished_at', 'notes', 'current_phase', 'current_issue'])
        step_log.log(
            ScanStepLog.Phase.SCAN_ERROR,
            f"Unexpected scan failure: {exc}",
            level=ScanStepLog.Level.ERROR,
        )
    finally:
        step_log.flush()
        _clear_stop_event(scan_task_id)
        close_old_connections()


def start_scan_task(scan_task_id: int) -> None:
    thread = threading.Thread(
        target=run_scan_task,
        args=(scan_task_id,),
        daemon=True,
        name=f'scan-task-{scan_task_id}',
    )
    thread.start()
