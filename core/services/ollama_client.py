from urllib.parse import urlparse

from ollama import Client, ResponseError


class OllamaServiceError(Exception):
    pass


def _to_dict(value):
    if isinstance(value, dict):
        return value

    model_dump = getattr(value, 'model_dump', None)
    if callable(model_dump):
        return model_dump()

    try:
        return dict(value)
    except Exception:
        return value


def _normalize_host(base_url: str) -> str:
    value = (base_url or '').strip()
    if not value:
        raise OllamaServiceError('Base URL is required.')

    parsed = urlparse(value)
    if not parsed.netloc:
        raise OllamaServiceError('Invalid Ollama base URL.')

    path = parsed.path.rstrip('/')
    if path.endswith('/v1'):
        path = path[:-3]

    if path and path != '/':
        return f'{parsed.scheme}://{parsed.netloc}{path}'

    return f'{parsed.scheme}://{parsed.netloc}'


def _candidate_hosts(base_url: str) -> list[str]:
    value = (base_url or '').strip()
    if not value:
        raise OllamaServiceError('Base URL is required.')

    if value.startswith(('http://', 'https://')):
        return [_normalize_host(value)]

    return [_normalize_host(f'https://{value}'), _normalize_host(f'http://{value}')]


def _build_client(host: str, api_key: str = '') -> Client:
    headers = {}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    return Client(host=host, headers=headers)


def list_models(base_url: str, api_key: str = '') -> list[str]:
    details = list_models_detailed(base_url=base_url, api_key=api_key)
    return sorted([model['name'] for model in details if model.get('name')])


def list_models_detailed(base_url: str, api_key: str = '') -> list[dict]:
    last_exception = None
    response = None

    for host in _candidate_hosts(base_url):
        client = _build_client(host, api_key)
        try:
            response = client.list()
            break
        except ResponseError as exc:
            raise OllamaServiceError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - retry fallback
            last_exception = exc

    if response is None:
        raise OllamaServiceError('Failed to fetch Ollama models.') from last_exception

    response_data = _to_dict(response)
    models = response_data.get('models', [])
    normalized = []
    for model in models:
        model_data = _to_dict(model)
        details = _to_dict(model_data.get('details', {}))
        raw_model = dict(model_data)
        if 'name' not in raw_model and raw_model.get('model'):
            raw_model['name'] = raw_model['model']
        if 'model' not in raw_model and raw_model.get('name'):
            raw_model['model'] = raw_model['name']

        normalized.append(
            {
                'name': model_data.get('model') or model_data.get('name'),
                'digest': model_data.get('digest'),
                'size': model_data.get('size'),
                'modified_at': model_data.get('modified_at'),
                'expires_at': model_data.get('expires_at'),
                'size_vram': model_data.get('size_vram'),
                'details': details,
                'format': details.get('format'),
                'family': details.get('family'),
                'families': details.get('families'),
                'parameter_size': details.get('parameter_size'),
                'quantization_level': details.get('quantization_level'),
                'parent_model': details.get('parent_model'),
                'raw': raw_model,
            }
        )

    return sorted(normalized, key=lambda item: item.get('name') or '')


def send_message(base_url: str, model: str, message: str, api_key: str = '') -> str:
    details = send_message_detailed(base_url=base_url, model=model, message=message, api_key=api_key)
    return details.get('content', '').strip()


def send_message_detailed(base_url: str, model: str, message: str, api_key: str = '') -> dict:
    if not model:
        raise OllamaServiceError('Model is required.')
    if not message:
        raise OllamaServiceError('Message is required.')

    last_exception = None
    response = None

    for host in _candidate_hosts(base_url):
        client = _build_client(host, api_key)
        try:
            response = client.chat(
                model=model,
                messages=[{'role': 'user', 'content': message}],
            )
            break
        except ResponseError as exc:
            raise OllamaServiceError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - retry fallback
            last_exception = exc

    if response is None:
        raise OllamaServiceError('Failed to send message to Ollama.') from last_exception

    response_data = _to_dict(response)
    message_data = _to_dict(response_data.get('message', {}))

    return {
        'model': response_data.get('model'),
        'created_at': response_data.get('created_at'),
        'done': response_data.get('done'),
        'done_reason': response_data.get('done_reason'),
        'total_duration': response_data.get('total_duration'),
        'load_duration': response_data.get('load_duration'),
        'prompt_eval_count': response_data.get('prompt_eval_count'),
        'prompt_eval_duration': response_data.get('prompt_eval_duration'),
        'eval_count': response_data.get('eval_count'),
        'eval_duration': response_data.get('eval_duration'),
        'role': message_data.get('role'),
        'content': message_data.get('content', ''),
        'thinking': message_data.get('thinking'),
        'tool_calls': message_data.get('tool_calls'),
        'images': message_data.get('images'),
        'message_raw': message_data,
        'raw': response_data,
    }
