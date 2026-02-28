import json
from urllib import error, request
from urllib.parse import urlencode


class GitHubServiceError(Exception):
    pass


def _api_get(path, api_key):
    url = f'https://api.github.com{path}'
    req = request.Request(
        url,
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {api_key}',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'issue-uploader',
        },
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
        raise GitHubServiceError(message) from exc
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
