import json
import time
from urllib import error, request


class Problem99ServiceError(Exception):
    pass


def upload_problem_direct(api_key: str, payload: dict) -> dict:
    url = 'https://api.problem99.io/api/direct/admin-upload'
    body = json.dumps(payload).encode('utf-8')
    req = request.Request(
        url,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authorization': f'Bearer {api_key}',
            'User-Agent': 'issue-uploader',
        },
        method='POST',
    )

    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=20) as response:
                raw = response.read().decode('utf-8')
                if not raw:
                    return {'ok': True}
                data = json.loads(raw)
                if isinstance(data, dict) and data.get('success') is False:
                    message = data.get('error') or data.get('message') or 'Problem99 rejected the upload payload.'
                    raise Problem99ServiceError(message)
                return data
        except error.HTTPError as exc:
            details = ''
            status_code = exc.code
            try:
                response_payload = json.loads(exc.read().decode('utf-8'))
                details = response_payload.get('message') or response_payload.get('error') or ''
            except Exception:
                details = ''

            retryable = status_code in {408, 425, 429, 500, 502, 503, 504}
            if retryable and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue

            message = details or f'Problem99 API request failed ({status_code}).'
            raise Problem99ServiceError(message) from exc
        except Problem99ServiceError:
            raise
        except Exception as exc:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise Problem99ServiceError('Could not connect to Problem99 API.') from exc

    raise Problem99ServiceError('Could not upload to Problem99 after retries.')
