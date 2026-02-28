from django.db import models


class AgentConfig(models.Model):
    class LLMProvider(models.TextChoices):
        OLLAMA = 'ollama', 'Ollama'
        VLLM = 'vllm', 'vLLM'

    name = models.CharField(max_length=100, unique=True, default='default')
    github_api_key = models.CharField(max_length=255)
    llm_provider = models.CharField(max_length=20, choices=LLMProvider.choices, default=LLMProvider.OLLAMA)
    llm_base_url = models.URLField(default='http://localhost:11434/v1')
    llm_model = models.CharField(max_length=120, default='llama3.1:8b')
    llm_api_key = models.CharField(max_length=255, blank=True)
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.20)
    max_issues_per_scan = models.PositiveIntegerField(default=200)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_llm_provider_display()})"


class Repository(models.Model):
    owner = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    full_name = models.CharField(max_length=255, unique=True)
    html_url = models.URLField(blank=True)
    default_branch = models.CharField(max_length=100, default='main')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['full_name']

    def __str__(self):
        return self.full_name


class ScanTask(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        STOPPED = 'stopped', 'Stopped'

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name='scan_tasks')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    prompt_model = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    total_issues = models.PositiveIntegerField(default=0)
    matched_issues = models.PositiveIntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Scan #{self.id} - {self.repository.full_name}"


class IssueCandidate(models.Model):
    class ResolutionStatus(models.TextChoices):
        NEW = 'new', 'New'
        REVIEWED = 'reviewed', 'Reviewed'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    scan_task = models.ForeignKey(ScanTask, on_delete=models.CASCADE, related_name='issue_candidates')
    issue_number = models.PositiveIntegerField()
    title = models.CharField(max_length=500)
    issue_url = models.URLField()
    state = models.CharField(max_length=20, default='open')
    linked_pr_url = models.URLField(blank=True)
    confidence_score = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    resolution_summary = models.TextField(blank=True)
    resolution_status = models.CharField(
        max_length=20,
        choices=ResolutionStatus.choices,
        default=ResolutionStatus.NEW,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-confidence_score', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['scan_task', 'issue_number'],
                name='unique_issue_number_per_scan',
            )
        ]

    def __str__(self):
        return f"#{self.issue_number} {self.title}"


class ScanIssueLog(models.Model):
    class Decision(models.TextChoices):
        INCLUDED = 'included', 'Included'
        SKIPPED = 'skipped', 'Skipped'
        ERROR = 'error', 'Error'

    scan_task = models.ForeignKey(ScanTask, on_delete=models.CASCADE, related_name='issue_logs')
    issue_number = models.PositiveIntegerField()
    title = models.CharField(max_length=500)
    issue_url = models.URLField()
    decision = models.CharField(max_length=20, choices=Decision.choices, default=Decision.SKIPPED)
    confidence_score = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['issue_number', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['scan_task', 'issue_number'],
                name='unique_issue_log_per_scan',
            )
        ]

    def __str__(self):
        return f"Scan #{self.scan_task_id} issue #{self.issue_number} ({self.decision})"
