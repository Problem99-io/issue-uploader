import logging
import math

from django.http import JsonResponse
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import AgentConfigForm, GlobalSettingsForm, RepositoryImportForm, ScanTaskForm
from .models import AgentConfig, GlobalSettings, IssueCandidate, Repository, ScanStepLog, ScanTask
from .services.github_client import GitHubServiceError, get_repository_by_full_name, validate_github_api_key
from .services.ollama_client import OllamaServiceError, list_models, send_message
from .services.scan_runner import request_stop_scan_task, start_scan_task


logger = logging.getLogger(__name__)


def _compute_throughput(scan_task):
    """Compute issues/min and uploads/min for a scan task. Returns (issues_per_min, uploads_per_min) or (None, None)."""
    if not scan_task.started_at or scan_task.processed_issues == 0:
        return None, None

    end_time = scan_task.finished_at or timezone.now()
    elapsed_seconds = (end_time - scan_task.started_at).total_seconds()
    if elapsed_seconds < 1:
        return None, None

    elapsed_minutes = elapsed_seconds / 60.0
    issues_per_min = round(scan_task.processed_issues / elapsed_minutes, 1)
    uploads_per_min = round(scan_task.uploaded_issues / elapsed_minutes, 1) if scan_task.uploaded_issues else None
    return issues_per_min, uploads_per_min


def _debug_print(message: str) -> None:
    print(f'[OLLAMA_DEBUG] {message}', flush=True)


def _upstream_auth_headers(request) -> dict[str, str]:
    header_map = {
        'HTTP_AUTHORIZATION': 'Authorization',
        'HTTP_X_PREVIEW_TOKEN': 'X-Preview-Token',
        'HTTP_X_OPENCODE_PREVIEW_TOKEN': 'X-Opencode-Preview-Token',
        'HTTP_X_FORWARDED_ACCESS_TOKEN': 'X-Forwarded-Access-Token',
    }
    headers = {}
    for meta_key, header_name in header_map.items():
        value = request.META.get(meta_key)
        if value:
            headers[header_name] = value
    return headers


def home(request):
    repository_count = Repository.objects.count()
    scan_count = ScanTask.objects.count()
    candidate_count = IssueCandidate.objects.count()
    latest_scans = ScanTask.objects.select_related('repository')[:5]

    context = {
        'repository_count': repository_count,
        'scan_count': scan_count,
        'candidate_count': candidate_count,
        'latest_scans': latest_scans,
    }
    return render(request, 'core/home.html', context)


def repository_list_create(request):
    repositories = Repository.objects.annotate(scan_count=Count('scan_tasks')).order_by('full_name')
    global_settings, _ = GlobalSettings.objects.get_or_create(name='default')
    github_key = (global_settings.github_api_key or '').strip()

    settings_form = GlobalSettingsForm(instance=global_settings)
    repo_form = RepositoryImportForm()

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'save-global-settings':
            settings_form = GlobalSettingsForm(request.POST, instance=global_settings)
            if settings_form.is_valid():
                github_key = settings_form.cleaned_data['github_api_key'].strip()
                if github_key:
                    try:
                        validate_github_api_key(github_key)
                    except GitHubServiceError as exc:
                        settings_form.add_error('github_api_key', str(exc))
                    else:
                        settings_form.save()
                        return redirect('repositories')
                else:
                    settings_form.save()
                    return redirect('repositories')

        elif action == 'add-repository':
            repo_form = RepositoryImportForm(request.POST)
            if not github_key:
                settings_form.add_error('github_api_key', 'Add a GitHub API key before adding repositories.')
            elif repo_form.is_valid():
                try:
                    repo_data = get_repository_by_full_name(
                        api_key=github_key,
                        full_name=repo_form.cleaned_data['full_name'],
                    )
                except GitHubServiceError as exc:
                    repo_form.add_error('full_name', str(exc))
                else:
                    Repository.objects.update_or_create(
                        full_name=repo_data['full_name'],
                        defaults={
                            'owner': repo_data['owner'],
                            'name': repo_data['name'],
                            'html_url': repo_data['html_url'],
                            'default_branch': repo_data['default_branch'],
                            'is_active': repo_form.cleaned_data['is_active'],
                        },
                    )
                    return redirect('repositories')

    context = {
        'repositories': repositories,
        'settings_form': settings_form,
        'repo_form': repo_form,
        'has_github_key': bool(github_key),
    }
    return render(request, 'core/repositories.html', context)


def scan_task_list_create(request):
    scan_tasks = ScanTask.objects.select_related('repository').all()

    if request.method == 'POST':
        action = request.POST.get('action', 'create-task')

        if action == 'rerun-all':
            rerunnable_tasks = ScanTask.objects.exclude(status=ScanTask.Status.RUNNING)
            for task in rerunnable_tasks:
                start_scan_task(task.id)
            return redirect('scan-tasks')

        form = ScanTaskForm(request.POST)
        if form.is_valid():
            scan_task = form.save()
            start_scan_task(scan_task.id)
            return redirect('scan-task-detail', task_id=scan_task.id)
    else:
        form = ScanTaskForm()

    context = {'scan_tasks': scan_tasks, 'form': form}
    return render(request, 'core/scan_tasks.html', context)


def task_manager(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')
        task_id = request.POST.get('task_id', '').strip()

        if action == 'rerun-all':
            for task in ScanTask.objects.exclude(status=ScanTask.Status.RUNNING):
                start_scan_task(task.id)
            return redirect('task-manager')

        if not task_id.isdigit():
            return redirect('task-manager')

        scan_task = get_object_or_404(ScanTask, pk=int(task_id))

        if action in {'start-task', 'restart-task'} and scan_task.status != ScanTask.Status.RUNNING:
            scan_task.cancel_requested = False
            scan_task.save(update_fields=['cancel_requested'])
            start_scan_task(scan_task.id)
        elif action == 'stop-task' and scan_task.status == ScanTask.Status.RUNNING:
            scan_task.cancel_requested = True
            scan_task.status = ScanTask.Status.STOPPED
            scan_task.finished_at = timezone.now()
            scan_task.save(update_fields=['cancel_requested', 'status', 'finished_at'])
            request_stop_scan_task(scan_task.id)
        elif action == 'remove-task' and scan_task.status != ScanTask.Status.RUNNING:
            scan_task.delete()

        return redirect('task-manager')

    tasks = ScanTask.objects.select_related('repository').all()
    return render(request, 'core/task_manager.html', {'tasks': tasks})


@require_GET
def task_manager_table_partial(request):
    tasks = ScanTask.objects.select_related('repository').all()
    return render(request, 'core/partials/task_manager_table.html', {'tasks': tasks})


def issue_candidate_list(request):
    scan_task_id = request.GET.get('scan_task')
    candidates = IssueCandidate.objects.select_related('scan_task', 'scan_task__repository')

    if scan_task_id:
        candidates = candidates.filter(scan_task_id=scan_task_id)

    context = {
        'candidates': candidates,
        'scan_tasks': ScanTask.objects.select_related('repository').all(),
        'active_scan_task_id': scan_task_id,
    }
    return render(request, 'core/issue_candidates.html', context)


def scan_task_detail(request, task_id):
    scan_task = get_object_or_404(ScanTask.objects.select_related('repository'), pk=task_id)
    candidates = scan_task.issue_candidates.all()
    issue_logs = scan_task.issue_logs.all()
    step_logs = scan_task.step_logs.order_by('-created_at')[:100]
    issues_per_min, uploads_per_min = _compute_throughput(scan_task)
    context = {
        'scan_task': scan_task,
        'candidates': candidates,
        'issue_logs': issue_logs,
        'step_logs': step_logs,
        'throughput_issues_per_min': issues_per_min,
        'throughput_uploads_per_min': uploads_per_min,
    }
    return render(request, 'core/scan_task_detail.html', context)


@require_GET
def scan_task_table_partial(request):
    scan_tasks = ScanTask.objects.select_related('repository').all()
    return render(request, 'core/partials/scan_task_table.html', {'scan_tasks': scan_tasks})


@require_GET
def scan_task_live_partial(request, task_id):
    scan_task = get_object_or_404(ScanTask.objects.select_related('repository'), pk=task_id)
    candidates = scan_task.issue_candidates.all()
    issue_logs = scan_task.issue_logs.all()
    step_logs = scan_task.step_logs.order_by('-created_at')[:100]
    issues_per_min, uploads_per_min = _compute_throughput(scan_task)
    context = {
        'scan_task': scan_task,
        'candidates': candidates,
        'issue_logs': issue_logs,
        'step_logs': step_logs,
        'throughput_issues_per_min': issues_per_min,
        'throughput_uploads_per_min': uploads_per_min,
    }
    return render(request, 'core/partials/scan_task_live.html', context)


def htmx_status(request):
    context = {'now': timezone.now()}
    return render(request, 'core/partials/status.html', context)


@require_GET
def scan_task_step_logs_partial(request, task_id):
    scan_task = get_object_or_404(ScanTask, pk=task_id)
    step_logs = scan_task.step_logs.order_by('-created_at')[:100]
    context = {'scan_task': scan_task, 'step_logs': step_logs}
    return render(request, 'core/partials/scan_step_logs.html', context)


def agent_config_list_create(request):
    configs = AgentConfig.objects.order_by('-updated_at')
    active_config = configs.first()
    global_settings, _ = GlobalSettings.objects.get_or_create(name='default')
    loaded_models = []
    model_load_status = ''

    if request.method == 'POST':
        form = AgentConfigForm(request.POST)
        action = request.POST.get('action', 'save-config')

        if action == 'load-models':
            base_url = (global_settings.ollama_base_url or '').strip()
            upstream_headers = _upstream_auth_headers(request)

            _debug_print(
                (
                    f"load-models action remote={request.META.get('REMOTE_ADDR')} "
                    f"base_url={base_url!r} "
                    f"forward_headers={sorted(upstream_headers.keys())}"
                )
            )

            try:
                loaded_models = list_models(
                    base_url=base_url,
                    extra_headers=upstream_headers,
                )
                if loaded_models:
                    model_load_status = f'Loaded {len(loaded_models)} model(s).'
                else:
                    model_load_status = 'No models returned.'
            except OllamaServiceError as exc:
                model_load_status = str(exc)
                _debug_print(f'load-models action service error: {exc!r}')
            except Exception as exc:  # pragma: no cover - debug safeguard
                model_load_status = f'Unexpected server error: {exc}'
                _debug_print(f'load-models action unexpected error: {exc!r}')
        else:
            if form.is_valid():
                config = active_config
                if config is None:
                    config = AgentConfig.objects.create(name='default', llm_model=form.cleaned_data['llm_model'], is_active=True)
                else:
                    config.llm_model = form.cleaned_data['llm_model']
                    config.is_active = True
                    config.save(update_fields=['llm_model', 'is_active', 'updated_at'])
                AgentConfig.objects.exclude(pk=config.pk).update(is_active=False)
                return redirect('agent-configs')
    else:
        form = AgentConfigForm(instance=active_config)

    context = {
        'configs': configs,
        'active_config': active_config,
        'form': form,
        'loaded_models': loaded_models,
        'model_load_status': model_load_status,
        'ollama_base_url': global_settings.ollama_base_url,
    }
    return render(request, 'core/agent_configs.html', context)


@require_GET
def agent_config_models(request):
    base_url = request.GET.get('base_url', '')
    upstream_headers = _upstream_auth_headers(request)

    _debug_print(
        (
            f"agent_config_models remote={request.META.get('REMOTE_ADDR')} "
            f"base_url={base_url!r} "
            f"forward_headers={sorted(upstream_headers.keys())}"
        )
    )
    logger.warning(
        'agent_config_models request: remote=%s base_url=%r has_api_key=%s',
        request.META.get('REMOTE_ADDR'),
        base_url,
        False,
    )

    try:
        models = list_models(base_url=base_url, extra_headers=upstream_headers)
    except OllamaServiceError as exc:
        _debug_print(f'agent_config_models service error: {exc!r}')
        logger.exception('agent_config_models service error for base_url=%r', base_url)
        return JsonResponse({'ok': False, 'error': str(exc), 'models': []}, status=400)
    except Exception as exc:  # pragma: no cover - debug safeguard
        _debug_print(f'agent_config_models unexpected error: {exc!r}')
        logger.exception('agent_config_models unexpected error for base_url=%r', base_url)
        return JsonResponse({'ok': False, 'error': f'Unexpected server error: {exc}', 'models': []}, status=500)

    return JsonResponse({'ok': True, 'models': models})


@require_POST
def agent_config_test_message(request):
    base_url = request.POST.get('base_url', '')
    model = request.POST.get('model', '')
    message = request.POST.get('message', '').strip()
    upstream_headers = _upstream_auth_headers(request)

    try:
        reply = send_message(
            base_url=base_url,
            model=model,
            message=message,
            extra_headers=upstream_headers,
        )
    except OllamaServiceError as exc:
        _debug_print(f'agent_config_test_message service error: {exc!r}')
        logger.exception('agent_config_test_message service error for base_url=%r model=%r', base_url, model)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Exception as exc:  # pragma: no cover - debug safeguard
        _debug_print(f'agent_config_test_message unexpected error: {exc!r}')
        logger.exception('agent_config_test_message unexpected error for base_url=%r model=%r', base_url, model)
        return JsonResponse({'ok': False, 'error': f'Unexpected server error: {exc}'}, status=500)

    return JsonResponse({'ok': True, 'reply': reply})
