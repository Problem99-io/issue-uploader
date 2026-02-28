from django import forms

from .models import AgentConfig, Repository, ScanTask


class GitHubApiKeyForm(forms.Form):
    github_api_key = forms.CharField(
        label='GitHub API key',
        widget=forms.PasswordInput(render_value=True),
    )


class RepositoryForm(forms.ModelForm):
    class Meta:
        model = Repository
        fields = ['owner', 'name', 'full_name', 'html_url', 'default_branch', 'is_active']


class RepositoryImportForm(forms.Form):
    full_name = forms.CharField(
        label='Repository (owner/name)',
        max_length=255,
        help_text='Example: octocat/Hello-World',
    )
    is_active = forms.BooleanField(label='Track this repository', required=False, initial=True)

    def clean_full_name(self):
        full_name = (self.cleaned_data.get('full_name') or '').strip()
        if full_name.count('/') != 1:
            raise forms.ValidationError('Use the format owner/name.')
        owner, name = full_name.split('/', 1)
        owner = owner.strip()
        name = name.strip()
        if not owner or not name:
            raise forms.ValidationError('Use the format owner/name.')
        return f'{owner}/{name}'


class ScanTaskForm(forms.ModelForm):
    class Meta:
        model = ScanTask
        fields = ['repository', 'prompt_model', 'notes']


class AgentConfigForm(forms.ModelForm):
    class Meta:
        model = AgentConfig
        fields = [
            'name',
            'github_api_key',
            'llm_provider',
            'llm_base_url',
            'llm_model',
            'llm_api_key',
            'temperature',
            'max_issues_per_scan',
            'is_active',
        ]
        widgets = {
            'github_api_key': forms.PasswordInput(render_value=True),
            'llm_api_key': forms.PasswordInput(render_value=True),
        }

    def clean_llm_base_url(self):
        llm_base_url = (self.cleaned_data.get('llm_base_url') or '').strip()
        if llm_base_url and not llm_base_url.startswith(('http://', 'https://')):
            llm_base_url = f'https://{llm_base_url}'
        return llm_base_url
