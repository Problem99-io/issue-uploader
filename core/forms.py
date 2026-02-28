from django import forms

from .models import AgentConfig, Repository, ScanTask


class RepositoryForm(forms.ModelForm):
    class Meta:
        model = Repository
        fields = ['owner', 'name', 'full_name', 'html_url', 'default_branch', 'is_active']


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
