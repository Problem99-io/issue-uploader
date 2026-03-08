import json
from urllib import error, request
from urllib.parse import urlencode


class GitHubServiceError(Exception):
    pass


class GitHubRateLimitError(GitHubServiceError):
    """Raised when GitHub returns a rate limit (403/429) response."""
    pass


def _api_get(path, api_key, extra_headers=None):
    url = f'https://api.github.com{path}'
    headers = {
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {api_key}',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'issue-uploader',
    }
    headers.update(extra_headers or {})
    req = request.Request(
        url,
        headers=headers,
        method='GET',
    )

    try:
        with request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode('utf-8'))
    except error.HTTPError as exc:
        details = ''
        try:
            payload = json.loads(exc.read().decode('utf-8'))
            details = payload.get('message', '')
        except Exception:
            details = ''
        message = details or f'GitHub API request failed ({exc.code}).'
        if exc.code in (403, 429) and 'rate limit' in message.lower():
            raise GitHubRateLimitError(message) from exc
        raise GitHubServiceError(message) from exc
    except GitHubRateLimitError:
        raise
    except Exception as exc:
        raise GitHubServiceError('Could not connect to GitHub API.') from exc


def validate_github_api_key(api_key):
    payload = _api_get('/user', api_key)
    login = payload.get('login')
    if not login:
        raise GitHubServiceError('GitHub key is valid but user details were missing.')
    return login


def get_repository_by_full_name(api_key, full_name):
    payload = _api_get(f'/repos/{full_name}', api_key)

    owner = (payload.get('owner') or {}).get('login')
    name = payload.get('name')
    repo_full_name = payload.get('full_name')

    if not owner or not name or not repo_full_name:
        raise GitHubServiceError('GitHub response is missing repository data.')

    return {
        'owner': owner,
        'name': name,
        'full_name': repo_full_name,
        'html_url': payload.get('html_url', ''),
        'default_branch': payload.get('default_branch') or 'main',
    }


def list_repository_issues(api_key, full_name, state='open', per_page=100, page=1):
    query = urlencode(
        {
            'state': state,
            'per_page': per_page,
            'page': page,
            'sort': 'updated',
            'direction': 'desc',
        }
    )
    payload = _api_get(f'/repos/{full_name}/issues?{query}', api_key)
    issues = []
    for item in payload:
        issues.append(
            {
                'number': item.get('number'),
                'title': item.get('title') or '',
                'body': item.get('body') or '',
                'state': item.get('state') or 'open',
                'html_url': item.get('html_url') or '',
                'labels': [label.get('name', '') for label in item.get('labels', []) if isinstance(label, dict)],
                'is_pull_request': bool(item.get('pull_request')),
            }
        )
    return issues


def list_issue_comments(api_key, full_name, issue_number, per_page=20, page=1):
    query = urlencode(
        {
            'per_page': per_page,
            'page': page,
            'sort': 'updated',
            'direction': 'desc',
        }
    )
    payload = _api_get(f'/repos/{full_name}/issues/{issue_number}/comments?{query}', api_key)
    comments = []
    for item in payload:
        comments.append(
            {
                'body': (item or {}).get('body') or '',
            }
        )
    return comments


def get_issue_timeline(api_key, full_name, issue_number, per_page=100, page=1):
    query = urlencode(
        {
            'per_page': per_page,
            'page': page,
        }
    )
    payload = _api_get(
        f'/repos/{full_name}/issues/{issue_number}/timeline?{query}',
        api_key,
        extra_headers={'Accept': 'application/vnd.github+json'},
    )
    return payload if isinstance(payload, list) else []


def find_closing_pull_request_number(api_key, full_name, issue_number):
    timeline = get_issue_timeline(api_key, full_name, issue_number)
    for event in timeline:
        source_issue = ((event or {}).get('source') or {}).get('issue') or {}
        pull_request = source_issue.get('pull_request') or {}
        pr_url = pull_request.get('url') or ''
        if '/pulls/' in pr_url:
            try:
                return int(pr_url.rstrip('/').split('/pulls/')[1])
            except Exception:
                continue
    return None


def get_pull_request(api_key, full_name, pull_number):
    payload = _api_get(f'/repos/{full_name}/pulls/{pull_number}', api_key)
    return {
        'number': payload.get('number'),
        'title': payload.get('title') or '',
        'body': payload.get('body') or '',
        'html_url': payload.get('html_url') or '',
        'merged_at': payload.get('merged_at'),
    }


def list_pull_request_files(api_key, full_name, pull_number, per_page=100, page=1):
    query = urlencode({'per_page': per_page, 'page': page})
    payload = _api_get(f'/repos/{full_name}/pulls/{pull_number}/files?{query}', api_key)
    files = []
    for item in payload:
        files.append(
            {
                'filename': item.get('filename') or '',
                'status': item.get('status') or '',
                'patch': item.get('patch') or '',
            }
        )
    return files
