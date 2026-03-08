from django.db import migrations, models


def _copy_agent_config_to_global_settings(apps, schema_editor):
    AgentConfig = apps.get_model('core', 'AgentConfig')
    GlobalSettings = apps.get_model('core', 'GlobalSettings')

    latest = AgentConfig.objects.order_by('-updated_at').first()
    defaults = {
        'github_api_key': '',
        'problem99_api_key': '',
        'ollama_base_url': 'http://localhost:11434',
    }
    if latest is not None:
        defaults['github_api_key'] = getattr(latest, 'github_api_key', '') or ''
        defaults['ollama_base_url'] = getattr(latest, 'llm_base_url', '') or defaults['ollama_base_url']

    GlobalSettings.objects.update_or_create(name='default', defaults=defaults)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_scanissuelog'),
    ]

    operations = [
        migrations.CreateModel(
            name='GlobalSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(default='default', max_length=100, unique=True)),
                ('github_api_key', models.CharField(blank=True, max_length=255)),
                ('problem99_api_key', models.CharField(blank=True, max_length=255)),
                ('ollama_base_url', models.URLField(default='http://localhost:11434')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.RunPython(_copy_agent_config_to_global_settings, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='agentconfig',
            name='github_api_key',
        ),
        migrations.RemoveField(
            model_name='agentconfig',
            name='llm_api_key',
        ),
        migrations.RemoveField(
            model_name='agentconfig',
            name='llm_base_url',
        ),
        migrations.RemoveField(
            model_name='agentconfig',
            name='llm_provider',
        ),
        migrations.RemoveField(
            model_name='agentconfig',
            name='max_issues_per_scan',
        ),
    ]
