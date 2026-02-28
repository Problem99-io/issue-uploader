from core.models import AgentConfig


def get_active_agent_config():
    return AgentConfig.objects.filter(is_active=True).order_by('-updated_at').first()
