import threading
import json
import re
from decimal import Decimal

from django.db import close_old_connections
from django.utils import timezone

from core.models import AgentConfig, IssueCandidate, ScanIssueLog, ScanTask
from core.services.agent_config import get_active_agent_config
from core.services.github_client import GitHubServiceError, list_issue_comments, list_repository_issues
from core.services.ollama_client import send_message


class ScanRunnerError(Exception):
    pass


class ScanRunnerStopped(ScanRunnerError):
    pass


_RUNNER_LOCK = threading.Lock()
_STOP_EVENTS: dict[int, threading.Event] = {}


def _sanitize_text(value: str) -> str:
    text = (value or '').strip()
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[REDACTED_EMAIL]', text)
    text = re.sub(r'@[A-Za-z0-9_-]+', '@user', text)
    text = re.sub(r'https?://\S+', '[REFERENCE_URL]', text)
    return text


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
    active_config: AgentConfig,
    model: str,
    temperature: float,
) -> tuple[bool, Decimal, str, bool, str]:
    if active_config.llm_provider != AgentConfig.LLMProvider.OLLAMA:
        return False, Decimal('0.00'), '', False, 'provider_not_ollama'
    if not active_config.llm_base_url or not model:
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

    prompt = (
        'You are an issue triage assistant.\\n'
        'Step 1: Check whether this issue has a clear error/problem and a clear solution in the issue body or comments '\
        '(including fixes explained in comments, commits, or PR references).\\n'
        'Step 2: If yes, produce structured, reusable output without names or personal information.\\n'
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
        f"Comments:\\n{comments_block}"
    )

    try:
        reply = send_message(
            base_url=active_config.llm_base_url,
            model=model,
            message=prompt,
            api_key=active_config.llm_api_key,
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
        error_message = _sanitize_text(
            str(data.get('error_message') or data.get('error_title_or_code') or data.get('error_code') or '')
        )
        solution_code = _sanitize_text(str(data.get('solution_code') or ''))
        framework = _sanitize_text(str(data.get('framework') or ''))
        language = _sanitize_text(str(data.get('language') or ''))
        explanation = _sanitize_text(str(data.get('explanation') or ''))
        tags = data.get('tags') or []
    except Exception:
        return False, Decimal('0.00'), '', False, 'invalid_confidence_reply'

    if confidence < Decimal('0.00'):
        confidence = Decimal('0.00')
    if confidence > Decimal('0.99'):
        confidence = Decimal('0.99')

    has_required_fields = all([error_message, solution_code, framework, language, explanation])
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
    scan_task.total_issues = 0
    scan_task.matched_issues = 0
    scan_task.save(
        update_fields=['status', 'started_at', 'finished_at', 'cancel_requested', 'total_issues', 'matched_issues']
    )
    IssueCandidate.objects.filter(scan_task=scan_task).delete()
    ScanIssueLog.objects.filter(scan_task=scan_task).delete()

    try:
        active_config = get_active_agent_config()
        if not active_config:
            raise ScanRunnerError('No active agent config. Configure one before running scans.')
        if not active_config.github_api_key:
            raise ScanRunnerError('Active agent config has no GitHub API key.')

        limit = max(1, min(active_config.max_issues_per_scan, 200))
        analysis_model = active_config.llm_model
        per_page = min(limit, 100)
        page = 1
        issues: list[dict] = []
        while len(issues) < limit:
            if _is_stop_requested(scan_task_id, stop_event):
                raise ScanRunnerStopped('Stopped by user.')
            page_items = list_repository_issues(
                api_key=active_config.github_api_key,
                full_name=scan_task.repository.full_name,
                state='open',
                per_page=per_page,
                page=page,
            )
            if not page_items:
                break
            issues.extend([item for item in page_items if not item.get('is_pull_request')])
            if len(page_items) < per_page:
                break
            page += 1

        issues = issues[:limit]
        scan_task.total_issues = len(issues)
        scan_task.save(update_fields=['total_issues'])

        matched_count = 0
        ai_attempted = 0
        ai_success = 0
        ai_failures: dict[str, int] = {}
        for issue in issues:
            if _is_stop_requested(scan_task_id, stop_event):
                raise ScanRunnerStopped('Stopped by user.')
            issue_number = issue.get('number')
            title = (issue.get('title') or '').strip()
            issue_url = issue.get('html_url') or ''
            if not issue_number or not title or not issue_url:
                continue

            comments = []
            try:
                comments = list_issue_comments(
                    api_key=active_config.github_api_key,
                    full_name=scan_task.repository.full_name,
                    issue_number=issue_number,
                    per_page=20,
                    page=1,
                )
            except Exception:
                comments = []

            ai_attempted += 1
            include, confidence, summary, ai_used, ai_error = _score_issue_with_ai(
                issue=issue,
                comments=comments,
                active_config=active_config,
                model=analysis_model,
                temperature=float(active_config.temperature),
            )
            if ai_used:
                ai_success += 1
            elif ai_error:
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
                continue

            if not include:
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
                continue

            IssueCandidate.objects.update_or_create(
                scan_task=scan_task,
                issue_number=issue_number,
                defaults={
                    'title': title,
                    'issue_url': issue_url,
                    'state': issue.get('state') or 'open',
                    'confidence_score': confidence,
                    'resolution_summary': summary,
                    'resolution_status': IssueCandidate.ResolutionStatus.NEW,
                },
            )
            matched_count += 1
            scan_task.matched_issues = matched_count
            scan_task.save(update_fields=['matched_issues'])
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

        if _is_stop_requested(scan_task_id, stop_event):
            raise ScanRunnerStopped('Stopped by user.')
        scan_task.matched_issues = matched_count
        scan_task.status = ScanTask.Status.COMPLETED
        scan_task.finished_at = timezone.now()
        scan_task.cancel_requested = False
        if ai_attempted:
            failure_parts = [f'{key}={value}' for key, value in sorted(ai_failures.items())]
            ai_note = f'AI triage attempts={ai_attempted}, success={ai_success}'
            if failure_parts:
                ai_note = f"{ai_note}, fallback_reasons=({', '.join(failure_parts)})"
            _append_error_note(scan_task, ai_note)
        scan_task.save(update_fields=['matched_issues', 'status', 'finished_at', 'cancel_requested', 'notes'])
    except ScanRunnerStopped as exc:
        _append_error_note(scan_task, str(exc))
        scan_task.status = ScanTask.Status.STOPPED
        scan_task.finished_at = timezone.now()
        scan_task.cancel_requested = False
        scan_task.save(update_fields=['status', 'finished_at', 'notes', 'cancel_requested'])
    except (GitHubServiceError, ScanRunnerError) as exc:
        _append_error_note(scan_task, f'Scan failed: {exc}')
        scan_task.status = ScanTask.Status.FAILED
        scan_task.finished_at = timezone.now()
        scan_task.save(update_fields=['status', 'finished_at', 'notes'])
    except Exception as exc:  # pragma: no cover - safeguard
        _append_error_note(scan_task, f'Unexpected scan failure: {exc}')
        scan_task.status = ScanTask.Status.FAILED
        scan_task.finished_at = timezone.now()
        scan_task.save(update_fields=['status', 'finished_at', 'notes'])
    finally:
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
