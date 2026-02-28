from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_alter_scantask_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScanIssueLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('issue_number', models.PositiveIntegerField()),
                ('title', models.CharField(max_length=500)),
                ('issue_url', models.URLField()),
                ('decision', models.CharField(choices=[('included', 'Included'), ('skipped', 'Skipped'), ('error', 'Error')], default='skipped', max_length=20)),
                ('confidence_score', models.DecimalField(decimal_places=2, default=0, max_digits=4)),
                ('reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('scan_task', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='issue_logs', to='core.scantask')),
            ],
            options={
                'ordering': ['issue_number', '-created_at'],
                'constraints': [models.UniqueConstraint(fields=('scan_task', 'issue_number'), name='unique_issue_log_per_scan')],
            },
        ),
    ]
