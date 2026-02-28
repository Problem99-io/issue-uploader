from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_agentconfig'),
    ]

    operations = [
        migrations.AddField(
            model_name='scantask',
            name='cancel_requested',
            field=models.BooleanField(default=False),
        ),
    ]
