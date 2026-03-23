from unittest.mock import MagicMock, call, patch

from django.test import TestCase
from django.urls import reverse

from core.forms import AgentConfigForm, GlobalSettingsForm
from core.models import AgentConfig, GlobalSettings, IssueCandidate, Repository, ScanTask
from core.services.ollama_client import (
    OllamaServiceError,
    list_models,
    list_models_detailed,
    send_message,
    send_message_detailed,
)
from core.services.problem99_client import Problem99ServiceError, upload_problem_direct
from core.services.scan_runner import run_scan_task


class AgentConfigFormTests(TestCase):
    def test_agent_config_form_accepts_model_only(self):
        form = AgentConfigForm(
            data={
                'llm_model': 'llama3.1:8b',
            }
        )

        self.assertTrue(form.is_valid())

    def test_global_settings_form_adds_https_when_missing(self):
        form = GlobalSettingsForm(
            data={
                'github_api_key': 'ghp_test',
                'problem99_api_key': 'p99_test',
                'ollama_base_url': 'olamm4-ext-1998.theunserisousram.xyz',
            }
        )

        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data['ollama_base_url'],
            'https://olamm4-ext-1998.theunserisousram.xyz',
        )


class OllamaServiceTests(TestCase):
    @patch('core.services.ollama_client.Client')
    def test_list_models_returns_sorted_names(self, client_cls):
        client = MagicMock()
        client.list.return_value = {
            'models': [
                {'model': 'llama3.2:3b'},
                {'name': 'llama3.1:8b'},
            ]
        }
        client_cls.return_value = client

        models = list_models('olamm4-ext-1998.theunserisousram.xyz')

        self.assertEqual(models, ['llama3.1:8b', 'llama3.2:3b'])
        client_cls.assert_called_once_with(
            host='https://olamm4-ext-1998.theunserisousram.xyz',
            headers={},
            timeout=300.0,
        )

    @patch('core.services.ollama_client.Client')
    def test_list_models_strips_v1_suffix(self, client_cls):
        client = MagicMock()
        client.list.return_value = {'models': []}
        client_cls.return_value = client

        list_models('https://example.com/v1')

        client_cls.assert_called_once_with(host='https://example.com', headers={}, timeout=300.0)

    @patch('core.services.ollama_client.Client')
    def test_list_models_strips_common_api_paths(self, client_cls):
        client = MagicMock()
        client.list.return_value = {'models': []}
        client_cls.return_value = client

        list_models('https://example.com/api/tags')

        client_cls.assert_called_once_with(host='https://example.com', headers={}, timeout=300.0)

    @patch('core.services.ollama_client.Client')
    def test_list_models_raises_service_error_on_failure(self, client_cls):
        client = MagicMock()
        client.list.side_effect = Exception('network down')
        client_cls.return_value = client

        with self.assertRaises(OllamaServiceError):
            list_models('https://example.com')


class Problem99ServiceTests(TestCase):
    @patch('core.services.problem99_client.request.urlopen')
    def test_upload_problem_direct_uses_admin_upload_endpoint(self, urlopen_mock):
        response = MagicMock()
        response.read.return_value = b'{"success": true, "solutionId": "abc123"}'
        urlopen_mock.return_value.__enter__.return_value = response

        payload = {
            'error_message': 'AUTH-401 Unauthorized: token validation failed.',
            'solution_code': 'return unauthorized_response()',
            'language': 'python',
        }
        upload_problem_direct('p99_admin_key', payload)

        req = urlopen_mock.call_args[0][0]
        self.assertEqual(req.full_url, 'https://api.problem99.io/api/direct/admin-upload')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.headers.get('Authorization'), 'Bearer p99_admin_key')
        self.assertEqual(req.headers.get('Content-type'), 'application/json')
        self.assertIsNone(req.headers.get('X-api-key'))

    @patch('core.services.problem99_client.request.urlopen')
    def test_upload_problem_direct_raises_on_success_false_response(self, urlopen_mock):
        response = MagicMock()
        response.read.return_value = b'{"success": false, "error": "Admin role required", "code": "FORBIDDEN"}'
        urlopen_mock.return_value.__enter__.return_value = response

        with self.assertRaises(Problem99ServiceError) as exc:
            upload_problem_direct(
                'p99_non_admin_key',
                {
                    'error_message': 'AUTH-401 Unauthorized: token validation failed.',
                    'solution_code': 'return unauthorized_response()',
                    'language': 'python',
                },
            )

        self.assertIn('Admin role required', str(exc.exception))

    @patch('core.services.ollama_client.Client')
    def test_send_message_returns_message_content(self, client_cls):
        client = MagicMock()
        client.chat.return_value = {'message': {'content': 'Hello from model'}}
        client_cls.return_value = client

        reply = send_message(
            base_url='https://example.com',
            model='llama3.1:8b',
            message='hi',
        )

        self.assertEqual(reply, 'Hello from model')
        client.chat.assert_called_once_with(
            model='llama3.1:8b',
            messages=[{'role': 'user', 'content': 'hi'}],
        )

    @patch('core.services.ollama_client.Client')
    def test_list_models_detailed_parses_metadata(self, client_cls):
        client = MagicMock()
        client.list.return_value = {
            'models': [
                {
                    'model': 'nemotron-3-nano:latest',
                    'digest': 'sha256:abc',
                    'size': 123,
                    'details': {
                        'family': 'nemotron',
                        'format': 'gguf',
                        'parameter_size': '3B',
                        'quantization_level': 'Q4_K_M',
                    },
                }
            ]
        }
        client_cls.return_value = client

        models = list_models_detailed('https://example.com')

        self.assertEqual(models[0]['name'], 'nemotron-3-nano:latest')
        self.assertEqual(models[0]['family'], 'nemotron')
        self.assertEqual(models[0]['format'], 'gguf')
        self.assertEqual(models[0]['parameter_size'], '3B')

    @patch('core.services.ollama_client.Client')
    def test_send_message_detailed_parses_response_metadata(self, client_cls):
        client = MagicMock()
        client.chat.return_value = {
            'model': 'nemotron-3-nano:latest',
            'created_at': '2026-02-28T00:00:00Z',
            'done': True,
            'done_reason': 'stop',
            'total_duration': 10,
            'message': {
                'role': 'assistant',
                'content': 'OLLAMA_OK',
            },
        }
        client_cls.return_value = client

        response = send_message_detailed(
            base_url='https://example.com',
            model='nemotron-3-nano:latest',
            message='hi',
        )

        self.assertEqual(response['model'], 'nemotron-3-nano:latest')
        self.assertEqual(response['role'], 'assistant')
        self.assertEqual(response['content'], 'OLLAMA_OK')
        self.assertEqual(response['done_reason'], 'stop')


class AgentConfigOllamaViewsTests(TestCase):
    @patch('core.views.list_models')
    def test_model_list_endpoint_returns_models(self, list_models_mock):
        list_models_mock.return_value = ['llama3.1:8b', 'llama3.2:3b']

        response = self.client.get(
            reverse('agent-config-models'),
            {'base_url': 'olamm4-ext-1998.theunserisousram.xyz'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {'ok': True, 'models': ['llama3.1:8b', 'llama3.2:3b']},
        )

    @patch('core.views.list_models')
    def test_model_list_endpoint_forwards_preview_header_to_upstream(self, list_models_mock):
        list_models_mock.return_value = []

        response = self.client.get(
            reverse('agent-config-models'),
            {'base_url': 'olamm4-ext-1998.theunserisousram.xyz'},
            HTTP_X_PREVIEW_TOKEN='preview-token-123',
        )

        self.assertEqual(response.status_code, 200)
        _, kwargs = list_models_mock.call_args
        self.assertEqual(
            kwargs.get('extra_headers'),
            {'X-Preview-Token': 'preview-token-123'},
        )

    @patch('core.views.list_models')
    def test_model_list_endpoint_handles_service_error(self, list_models_mock):
        list_models_mock.side_effect = OllamaServiceError('cannot connect')

        response = self.client.get(reverse('agent-config-models'), {'base_url': 'bad'})

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content,
            {'ok': False, 'error': 'cannot connect', 'models': []},
        )

    @patch('core.views.send_message')
    def test_test_message_endpoint_returns_reply(self, send_message_mock):
        send_message_mock.return_value = 'pong'

        response = self.client.post(
            reverse('agent-config-test-message'),
            {
                'base_url': 'olamm4-ext-1998.theunserisousram.xyz',
                'model': 'llama3.1:8b',
                'message': 'ping',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {'ok': True, 'reply': 'pong'})

    @patch('core.views.send_message')
    @patch('core.views.list_models')
    def test_full_select_model_then_chat_flow(self, list_models_mock, send_message_mock):
        list_models_mock.return_value = ['llama3.1:8b']
        send_message_mock.return_value = 'Hello'

        models_response = self.client.get(
            reverse('agent-config-models'),
            {'base_url': 'olamm4-ext-1998.theunserisousram.xyz'},
        )
        self.assertEqual(models_response.status_code, 200)

        chat_response = self.client.post(
            reverse('agent-config-test-message'),
            {
                'base_url': 'olamm4-ext-1998.theunserisousram.xyz',
                'model': 'llama3.1:8b',
                'message': 'Say hello',
            },
        )
        self.assertEqual(chat_response.status_code, 200)
        self.assertJSONEqual(chat_response.content, {'ok': True, 'reply': 'Hello'})


class RepositoryIntegrationTests(TestCase):
    def test_repository_page_disables_repository_submit_without_github_key(self):
        response = self.client.get(reverse('repositories'))

        self.assertContains(response, 'Add a GitHub API key first to unlock repository tracking.')
        self.assertContains(response, 'name="action" value="add-repository"')
        self.assertContains(response, 'disabled')

    @patch('core.views.validate_github_api_key')
    def test_save_global_settings_persists_token_values(self, validate_key_mock):
        validate_key_mock.return_value = 'octocat'

        response = self.client.post(
            reverse('repositories'),
            {
                'action': 'save-global-settings',
                'github_api_key': 'ghp_test',
                'problem99_api_key': 'p99_test',
                'ollama_base_url': 'http://localhost:11434',
            },
        )

        self.assertEqual(response.status_code, 302)
        settings = GlobalSettings.objects.get(name='default')
        self.assertEqual(settings.github_api_key, 'ghp_test')
        self.assertEqual(settings.problem99_api_key, 'p99_test')
        self.assertEqual(settings.ollama_base_url, 'http://localhost:11434')

    @patch('core.views.get_repository_by_full_name')
    def test_add_repository_imports_metadata_from_github(self, get_repo_mock):
        GlobalSettings.objects.update_or_create(
            name='default',
            defaults={
                'github_api_key': 'ghp_test',
                'problem99_api_key': 'p99_test',
                'ollama_base_url': 'http://localhost:11434',
            },
        )
        get_repo_mock.return_value = {
            'owner': 'octocat',
            'name': 'Hello-World',
            'full_name': 'octocat/Hello-World',
            'html_url': 'https://github.com/octocat/Hello-World',
            'default_branch': 'main',
        }

        response = self.client.post(
            reverse('repositories'),
            {
                'action': 'add-repository',
                'full_name': 'octocat/Hello-World',
                'is_active': 'on',
            },
        )

        self.assertEqual(response.status_code, 302)
        repo = Repository.objects.get(full_name='octocat/Hello-World')
        self.assertEqual(repo.owner, 'octocat')
        self.assertEqual(repo.name, 'Hello-World')
        self.assertEqual(repo.default_branch, 'main')


class ScanTaskExecutionTests(TestCase):
    @patch('core.views.start_scan_task')
    def test_create_scan_task_starts_background_runner(self, start_scan_task_mock):
        repo = Repository.objects.create(
            owner='octocat',
            name='Hello-World',
            full_name='octocat/Hello-World',
            html_url='https://github.com/octocat/Hello-World',
            default_branch='main',
            is_active=True,
        )

        response = self.client.post(
            reverse('scan-tasks'),
            {
                'repository': repo.id,
                'prompt_model': 'qwen3:30b',
                'notes': 'run scan',
            },
        )

        self.assertEqual(response.status_code, 302)
        scan_task = ScanTask.objects.get(repository=repo)
        start_scan_task_mock.assert_called_once_with(scan_task.id)

    @patch('core.views.start_scan_task')
    def test_rerun_all_tasks_starts_non_running_tasks(self, start_scan_task_mock):
        repo = Repository.objects.create(
            owner='octocat',
            name='Hello-World',
            full_name='octocat/Hello-World',
            html_url='https://github.com/octocat/Hello-World',
            default_branch='main',
            is_active=True,
        )
        t1 = ScanTask.objects.create(repository=repo, status=ScanTask.Status.PENDING)
        ScanTask.objects.create(repository=repo, status=ScanTask.Status.RUNNING)
        t3 = ScanTask.objects.create(repository=repo, status=ScanTask.Status.COMPLETED)

        response = self.client.post(reverse('scan-tasks'), {'action': 'rerun-all'})

        self.assertEqual(response.status_code, 302)
        start_scan_task_mock.assert_has_calls([call(t1.id), call(t3.id)], any_order=True)
        self.assertEqual(start_scan_task_mock.call_count, 2)

    @patch('core.services.scan_runner.find_closing_pull_request_number')
    @patch('core.services.scan_runner.send_message')
    @patch('core.services.scan_runner.upload_problem_direct')
    @patch('core.services.scan_runner.list_issue_comments')
    @patch('core.services.scan_runner.list_repository_issues')
    def test_run_scan_task_creates_candidates_and_uploads_to_problem99(
        self,
        list_issues_mock,
        list_comments_mock,
        upload_problem_direct_mock,
        send_message_mock,
        find_closing_pr_mock,
    ):
        find_closing_pr_mock.return_value = None
        repo = Repository.objects.create(
            owner='octocat',
            name='Hello-World',
            full_name='octocat/Hello-World',
            html_url='https://github.com/octocat/Hello-World',
            default_branch='main',
            is_active=True,
        )
        AgentConfig.objects.create(
            name='default',
            llm_model='qwen3:30b',
            is_active=True,
        )
        GlobalSettings.objects.update_or_create(
            name='default',
            defaults={
                'github_api_key': 'ghp_test',
                'problem99_api_key': 'p99_test',
                'ollama_base_url': 'http://localhost:11434',
            },
        )
        scan_task = ScanTask.objects.create(repository=repo, prompt_model='qwen3:30b')

        list_issues_mock.return_value = [
            {
                'number': 123,
                'title': 'Bug: crash on startup',
                'body': 'Steps to repro and traceback included.',
                'state': 'open',
                'html_url': 'https://github.com/octocat/Hello-World/issues/123',
                'labels': ['bug'],
            },
            {
                'number': 124,
                'title': 'Docs improvement',
                'body': 'Improve readme docs.',
                'state': 'open',
                'html_url': 'https://github.com/octocat/Hello-World/issues/124',
                'labels': ['documentation'],
            },
        ]
        list_comments_mock.return_value = [{'body': 'This is fixed in PR #123'}]
        send_message_mock.side_effect = [
            (
                '{"include": true, "confidence": 0.91, '
                '"error_message": "AUTH-401 Unauthorized: Token validation failed when accessing protected API endpoint", '
                '"solution_code": "if (!token || !isValidToken(token)) {\\n  return res.status(401).json({ error: \\"Unauthorized\\" });\\n}", '
                '"error_code": "AUTH-401", '
                '"framework": "express", '
                '"language": "javascript", '
                '"explanation": "The error occurs because the authentication middleware does not validate the token before allowing access to protected routes. The fix adds a guard clause that checks for token presence and validity, returning a 401 status when authentication fails.", '
                '"tags": ["auth", "token"], '
                '"alternative_solutions": ["Use signed session cookies"], '
                '"source": "issue-uploader"}'
            ),
            (
                '{"include": false, "confidence": 0.22, "error_message": "", "solution_code": "", '
                '"framework": "", "language": "", "explanation": "", "tags": []}'
            ),
        ]

        run_scan_task(scan_task.id)

        scan_task.refresh_from_db()
        self.assertEqual(scan_task.status, ScanTask.Status.COMPLETED)
        self.assertEqual(scan_task.total_issues, 2)
        self.assertEqual(scan_task.matched_issues, 1)
        self.assertTrue(scan_task.started_at)
        self.assertTrue(scan_task.finished_at)
        self.assertEqual(IssueCandidate.objects.filter(scan_task=scan_task).count(), 1)
        candidate = IssueCandidate.objects.get(scan_task=scan_task)
        self.assertEqual(str(candidate.confidence_score), '0.91')
        self.assertIn('"error_message":', candidate.resolution_summary)
        self.assertIn('"solution_code":', candidate.resolution_summary)
        self.assertIn('"tags": [', candidate.resolution_summary)
        upload_problem_direct_mock.assert_called_once()
        upload_call = upload_problem_direct_mock.call_args.kwargs
        self.assertEqual(upload_call['api_key'], 'p99_test')
        self.assertEqual(upload_call['payload']['error_code'], 'AUTH-401')
        self.assertEqual(upload_call['payload']['tags'], ['auth', 'token'])
        self.assertEqual(upload_call['payload']['alternative_solutions'], ['Use signed session cookies'])
        self.assertEqual(upload_call['payload']['source'], 'issue-uploader')

    @patch('core.services.scan_runner.find_closing_pull_request_number')
    @patch('core.services.scan_runner.send_message')
    @patch('core.services.scan_runner.list_issue_comments')
    @patch('core.services.scan_runner.list_repository_issues')
    def test_run_scan_task_continues_pagination_when_page_has_only_prs(
        self,
        list_issues_mock,
        list_comments_mock,
        send_message_mock,
        find_closing_pr_mock,
    ):
        find_closing_pr_mock.return_value = None
        repo = Repository.objects.create(
            owner='django',
            name='django',
            full_name='django/django',
            html_url='https://github.com/django/django',
            default_branch='main',
            is_active=True,
        )
        AgentConfig.objects.create(
            name='default',
            llm_model='qwen3:30b',
            is_active=True,
        )
        GlobalSettings.objects.update_or_create(
            name='default',
            defaults={
                'github_api_key': 'ghp_test',
                'problem99_api_key': 'p99_test',
                'ollama_base_url': 'http://localhost:11434',
            },
        )
        scan_task = ScanTask.objects.create(repository=repo, prompt_model='qwen3:30b')

        first_page = [
            {
                'number': idx,
                'title': f'PR {idx}',
                'body': 'pull request body',
                'state': 'open',
                'html_url': f'https://github.com/django/django/pull/{idx}',
                'labels': [],
                'is_pull_request': True,
            }
            for idx in range(1, 101)
        ]
        second_page = [
            {
                'number': 99999,
                'title': 'Bug: crash on sqlite migration',
                'body': 'traceback and repro steps included',
                'state': 'open',
                'html_url': 'https://github.com/django/django/issues/99999',
                'labels': ['bug'],
                'is_pull_request': False,
            }
        ]
        list_issues_mock.side_effect = [first_page, second_page]
        list_comments_mock.return_value = [{'body': 'Related fix PR #99999'}]
        send_message_mock.return_value = (
            '{"include": true, "confidence": 0.84, '
            '"error_message": "MIG-500 OperationalError: database table already exists during migration apply", '
            '"solution_code": "from django.db import transaction\\n\\nwith transaction.atomic():\\n    call_command(\\"migrate\\", \\"myapp\\", \\"0005\\")\\n    call_command(\\"migrate\\", \\"myapp\\")", '
            '"framework": "django", '
            '"language": "python", '
            '"explanation": "The error occurs when a migration partially fails, leaving the database in an inconsistent state. Wrapping migration steps in an atomic transaction ensures all-or-nothing execution, preventing partial schema changes that cause table-already-exists errors on retry.", '
            '"tags": ["migration", "database"]}'
        )

        run_scan_task(scan_task.id)

        scan_task.refresh_from_db()
        self.assertEqual(scan_task.status, ScanTask.Status.COMPLETED)
        self.assertEqual(scan_task.total_issues, 1)
        self.assertEqual(scan_task.matched_issues, 1)
        self.assertEqual(IssueCandidate.objects.filter(scan_task=scan_task).count(), 1)

    @patch('core.services.scan_runner.time.sleep')
    @patch('core.services.scan_runner.find_closing_pull_request_number')
    @patch('core.services.scan_runner.send_message')
    @patch('core.services.scan_runner.list_issue_comments')
    @patch('core.services.scan_runner.list_repository_issues')
    def test_run_scan_task_retries_after_github_rate_limit(
        self,
        list_issues_mock,
        list_comments_mock,
        send_message_mock,
        find_closing_pr_mock,
        sleep_mock,
    ):
        from core.services.github_client import GitHubRateLimitError

        repo = Repository.objects.create(
            owner='octocat',
            name='Hello-World',
            full_name='octocat/Hello-World',
            html_url='https://github.com/octocat/Hello-World',
            default_branch='main',
            is_active=True,
        )
        AgentConfig.objects.create(name='default', llm_model='qwen3:30b', is_active=True)
        GlobalSettings.objects.update_or_create(
            name='default',
            defaults={
                'github_api_key': 'ghp_test',
                'problem99_api_key': 'p99_test',
                'ollama_base_url': 'http://localhost:11434',
            },
        )
        scan_task = ScanTask.objects.create(repository=repo, prompt_model='qwen3:30b')

        list_issues_mock.return_value = [
            {
                'number': 42,
                'title': 'Bug: rate limit test',
                'body': 'Error details here.',
                'state': 'closed',
                'html_url': 'https://github.com/octocat/Hello-World/issues/42',
                'labels': ['bug'],
            },
        ]

        # First call to list_issue_comments hits rate limit, second call succeeds
        list_comments_mock.side_effect = [
            GitHubRateLimitError('API rate limit exceeded for user ID 12345.'),
            [{'body': 'Fixed by updating the config.'}],
        ]
        find_closing_pr_mock.return_value = None
        send_message_mock.return_value = (
            '{"include": true, "confidence": 0.92, '
            '"error_message": "CFG-500 ConfigError: missing required field in application configuration file", '
            '"solution_code": "config = load_config(path)\\nif not config.get(\\"required_field\\"):\\n    config[\\"required_field\\"] = default_value\\n    save_config(path, config)", '
            '"framework": "unknown", '
            '"language": "python", '
            '"explanation": "The error occurs because the configuration loader does not validate required fields before use. Adding a guard that checks for and sets a default value prevents the ConfigError from being raised during startup.", '
            '"tags": ["config", "validation"]}'
        )

        run_scan_task(scan_task.id)

        scan_task.refresh_from_db()
        self.assertEqual(scan_task.status, ScanTask.Status.COMPLETED)
        self.assertEqual(scan_task.total_issues, 1)
        self.assertEqual(scan_task.matched_issues, 1)
        # list_issue_comments called twice: first hit rate limit, second succeeded
        self.assertEqual(list_comments_mock.call_count, 2)
        # time.sleep was called during the 61-minute wait (in 30-second chunks)
        self.assertTrue(sleep_mock.call_count > 0)


class TaskManagerTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            owner='octocat',
            name='Hello-World',
            full_name='octocat/Hello-World',
            html_url='https://github.com/octocat/Hello-World',
            default_branch='main',
            is_active=True,
        )

    @patch('core.views.start_scan_task')
    def test_task_manager_start_and_restart(self, start_scan_task_mock):
        task = ScanTask.objects.create(repository=self.repo, status=ScanTask.Status.PENDING)

        response_start = self.client.post(reverse('task-manager'), {'action': 'start-task', 'task_id': task.id})
        response_restart = self.client.post(reverse('task-manager'), {'action': 'restart-task', 'task_id': task.id})

        self.assertEqual(response_start.status_code, 302)
        self.assertEqual(response_restart.status_code, 302)
        self.assertEqual(start_scan_task_mock.call_count, 2)

    @patch('core.views.request_stop_scan_task')
    def test_task_manager_stop_running_task(self, stop_scan_task_mock):
        task = ScanTask.objects.create(repository=self.repo, status=ScanTask.Status.RUNNING)

        response = self.client.post(reverse('task-manager'), {'action': 'stop-task', 'task_id': task.id})

        self.assertEqual(response.status_code, 302)
        stop_scan_task_mock.assert_called_once_with(task.id)
        task.refresh_from_db()
        self.assertEqual(task.status, ScanTask.Status.STOPPED)
        self.assertTrue(task.cancel_requested)

    def test_task_manager_remove_non_running_task(self):
        task = ScanTask.objects.create(repository=self.repo, status=ScanTask.Status.COMPLETED)

        response = self.client.post(reverse('task-manager'), {'action': 'remove-task', 'task_id': task.id})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ScanTask.objects.filter(pk=task.id).exists())
