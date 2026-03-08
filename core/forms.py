from django import forms

from .models import AgentConfig, GlobalSettings, Repository, ScanTask


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
            'llm_model',
        ]
        labels = {
            'llm_model': 'Model',
        }


class GlobalSettingsForm(forms.ModelForm):
    class Meta:
        model = GlobalSettings
        fields = [
            'github_api_key',
            'problem99_api_key',
            'ollama_base_url',
        ]
        widgets = {
            'github_api_key': forms.PasswordInput(render_value=True),
            'problem99_api_key': forms.PasswordInput(render_value=True),
        }
        labels = {
            'github_api_key': 'GitHub API key',
            'problem99_api_key': 'Problem99 API key',
            'ollama_base_url': 'Global Ollama URL',
        }

    def clean_ollama_base_url(self):
        ollama_base_url = (self.cleaned_data.get('ollama_base_url') or '').strip()
        if ollama_base_url and not ollama_base_url.startswith(('http://', 'https://')):
            ollama_base_url = f'https://{ollama_base_url}'
        return ollama_base_url
