from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.urls import reverse

from core.forms import AgentConfigForm
from core.services.ollama_client import (
    OllamaServiceError,
    list_models,
    list_models_detailed,
    send_message,
    send_message_detailed,
)


class AgentConfigFormTests(TestCase):
    def test_llm_base_url_adds_https_when_missing(self):
        form = AgentConfigForm(
            data={
                'name': 'cfg-1',
                'github_api_key': 'ghp_test',
                'llm_provider': 'ollama',
                'llm_base_url': 'olamm4-ext-1998.theunserisousram.xyz',
                'llm_model': 'llama3.1:8b',
                'llm_api_key': '',
                'temperature': '0.20',
                'max_issues_per_scan': 200,
                'is_active': True,
            }
        )

        self.assertTrue(form.is_valid())
        self.assertEqual(
            form.cleaned_data['llm_base_url'],
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
        )

    @patch('core.services.ollama_client.Client')
    def test_list_models_strips_v1_suffix(self, client_cls):
        client = MagicMock()
        client.list.return_value = {'models': []}
        client_cls.return_value = client

        list_models('https://example.com/v1')

        client_cls.assert_called_once_with(host='https://example.com', headers={})

    @patch('core.services.ollama_client.Client')
    def test_list_models_raises_service_error_on_failure(self, client_cls):
        client = MagicMock()
        client.list.side_effect = Exception('network down')
        client_cls.return_value = client

        with self.assertRaises(OllamaServiceError):
            list_models('https://example.com')

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
            {'base_url': 'olamm4-ext-1998.theunserisousram.xyz', 'api_key': ''},
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {'ok': True, 'models': ['llama3.1:8b', 'llama3.2:3b']},
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
