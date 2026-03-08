from core.models import AgentConfig, GlobalSettings


def get_active_agent_config():
    return AgentConfig.objects.filter(is_active=True).order_by('-updated_at').first()


def get_global_settings():
    settings, _ = GlobalSettings.objects.get_or_create(name='default')
    return settings
