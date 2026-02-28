from django.http import JsonResponse
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import AgentConfigForm, RepositoryForm, ScanTaskForm
from .models import AgentConfig, IssueCandidate, Repository, ScanTask
from .services.ollama_client import OllamaServiceError, list_models, send_message


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

    if request.method == 'POST':
        form = RepositoryForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('repositories')
    else:
        form = RepositoryForm()

    context = {'repositories': repositories, 'form': form}
    return render(request, 'core/repositories.html', context)


def scan_task_list_create(request):
    scan_tasks = ScanTask.objects.select_related('repository').all()

    if request.method == 'POST':
        form = ScanTaskForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('scan-tasks')
    else:
        form = ScanTaskForm()

    context = {'scan_tasks': scan_tasks, 'form': form}
    return render(request, 'core/scan_tasks.html', context)


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
    context = {'scan_task': scan_task, 'candidates': candidates}
    return render(request, 'core/scan_task_detail.html', context)


def htmx_status(request):
    context = {'now': timezone.now()}
    return render(request, 'core/partials/status.html', context)


def agent_config_list_create(request):
    configs = AgentConfig.objects.order_by('name')

    if request.method == 'POST':
        form = AgentConfigForm(request.POST)
        if form.is_valid():
            if form.cleaned_data['is_active']:
                AgentConfig.objects.update(is_active=False)
            form.save()
            return redirect('agent-configs')
    else:
        form = AgentConfigForm()

    context = {'configs': configs, 'form': form}
    return render(request, 'core/agent_configs.html', context)


@require_GET
def agent_config_models(request):
    base_url = request.GET.get('base_url', '')
    api_key = request.GET.get('api_key', '')

    try:
        models = list_models(base_url=base_url, api_key=api_key)
    except OllamaServiceError as exc:
        return JsonResponse({'ok': False, 'error': str(exc), 'models': []}, status=400)

    return JsonResponse({'ok': True, 'models': models})


@require_POST
def agent_config_test_message(request):
    base_url = request.POST.get('base_url', '')
    model = request.POST.get('model', '')
    message = request.POST.get('message', '').strip()
    api_key = request.POST.get('api_key', '')

    try:
        reply = send_message(base_url=base_url, model=model, message=message, api_key=api_key)
    except OllamaServiceError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    return JsonResponse({'ok': True, 'reply': reply})
