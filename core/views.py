import logging

from django.http import JsonResponse
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import AgentConfigForm, GitHubApiKeyForm, RepositoryImportForm, ScanTaskForm
from .models import AgentConfig, IssueCandidate, Repository, ScanTask
from .services.github_client import GitHubServiceError, get_repository_by_full_name, validate_github_api_key
from .services.ollama_client import OllamaServiceError, list_models, send_message
from .services.scan_runner import request_stop_scan_task, start_scan_task


logger = logging.getLogger(__name__)


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
    active_config = AgentConfig.objects.filter(is_active=True).first()
    github_key = (active_config.github_api_key if active_config else '').strip()

    key_form = GitHubApiKeyForm()
    repo_form = RepositoryImportForm()

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'save-github-key':
            key_form = GitHubApiKeyForm(request.POST)
            if key_form.is_valid():
                github_key = key_form.cleaned_data['github_api_key'].strip()
                try:
                    validate_github_api_key(github_key)
                except GitHubServiceError as exc:
                    key_form.add_error('github_api_key', str(exc))
                else:
                    if active_config is None:
                        active_config, _ = AgentConfig.objects.get_or_create(
                            name='default',
                            defaults={
                                'github_api_key': github_key,
                                'is_active': True,
                            },
                        )

                    active_config.github_api_key = github_key
                    active_config.is_active = True
                    active_config.save()
                    AgentConfig.objects.exclude(pk=active_config.pk).update(is_active=False)
                    return redirect('repositories')

        elif action == 'add-repository':
            repo_form = RepositoryImportForm(request.POST)
            if not github_key:
                key_form.add_error('github_api_key', 'Add a GitHub API key before adding repositories.')
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
        'key_form': key_form,
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
    context = {'scan_task': scan_task, 'candidates': candidates, 'issue_logs': issue_logs}
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
    context = {'scan_task': scan_task, 'candidates': candidates, 'issue_logs': issue_logs}
    return render(request, 'core/partials/scan_task_live.html', context)


def htmx_status(request):
    context = {'now': timezone.now()}
    return render(request, 'core/partials/status.html', context)


def agent_config_list_create(request):
    configs = AgentConfig.objects.order_by('name')
    loaded_models = []
    model_load_status = ''

    if request.method == 'POST':
        form = AgentConfigForm(request.POST)
        action = request.POST.get('action', 'save-config')

        if action == 'load-models':
            if form.is_valid():
                base_url = form.cleaned_data.get('llm_base_url', '')
                api_key = form.cleaned_data.get('llm_api_key', '')
                upstream_headers = _upstream_auth_headers(request)

                _debug_print(
                    (
                        f"load-models action remote={request.META.get('REMOTE_ADDR')} "
                        f"base_url={base_url!r} has_api_key={bool(api_key)} "
                        f"forward_headers={sorted(upstream_headers.keys())}"
                    )
                )

                try:
                    loaded_models = list_models(
                        base_url=base_url,
                        api_key=api_key,
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
                model_load_status = 'Fix form errors, then try loading models again.'
        else:
            if form.is_valid():
                if form.cleaned_data['is_active']:
                    AgentConfig.objects.update(is_active=False)
                form.save()
                return redirect('agent-configs')
    else:
        form = AgentConfigForm()

    context = {
        'configs': configs,
        'form': form,
        'loaded_models': loaded_models,
        'model_load_status': model_load_status,
    }
    return render(request, 'core/agent_configs.html', context)


@require_GET
def agent_config_models(request):
    base_url = request.GET.get('base_url', '')
    api_key = request.GET.get('api_key', '')
    upstream_headers = _upstream_auth_headers(request)

    _debug_print(
        (
            f"agent_config_models remote={request.META.get('REMOTE_ADDR')} "
            f"base_url={base_url!r} has_api_key={bool(api_key)} "
            f"forward_headers={sorted(upstream_headers.keys())}"
        )
    )
    logger.warning(
        'agent_config_models request: remote=%s base_url=%r has_api_key=%s',
        request.META.get('REMOTE_ADDR'),
        base_url,
        bool(api_key),
    )

    try:
        models = list_models(base_url=base_url, api_key=api_key, extra_headers=upstream_headers)
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
    api_key = request.POST.get('api_key', '')
    upstream_headers = _upstream_auth_headers(request)

    try:
        reply = send_message(
            base_url=base_url,
            model=model,
            message=message,
            api_key=api_key,
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
