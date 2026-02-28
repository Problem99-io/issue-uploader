from django.urls import path

from .views import (
    agent_config_models,
    agent_config_list_create,
    agent_config_test_message,
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
    path('agent-configs/models/', agent_config_models, name='agent-config-models'),
    path('agent-configs/test-message/', agent_config_test_message, name='agent-config-test-message'),
    path('repositories/', repository_list_create, name='repositories'),
    path('scan-tasks/', scan_task_list_create, name='scan-tasks'),
    path('scan-tasks/<int:task_id>/', scan_task_detail, name='scan-task-detail'),
    path('issue-candidates/', issue_candidate_list, name='issue-candidates'),
    path('htmx/status/', htmx_status, name='htmx-status'),
]
