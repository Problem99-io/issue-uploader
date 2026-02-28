from django.urls import path

from .views import (
    agent_config_list_create,
    home,
    htmx_status,
    issue_candidate_list,
    repository_list_create,
    scan_task_detail,
    scan_task_list_create,
)

urlpatterns = [
    path('', home, name='home'),
    path('agent-configs/', agent_config_list_create, name='agent-configs'),
    path('repositories/', repository_list_create, name='repositories'),
    path('scan-tasks/', scan_task_list_create, name='scan-tasks'),
    path('scan-tasks/<int:task_id>/', scan_task_detail, name='scan-task-detail'),
    path('issue-candidates/', issue_candidate_list, name='issue-candidates'),
    path('htmx/status/', htmx_status, name='htmx-status'),
]
