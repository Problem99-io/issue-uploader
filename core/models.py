from django.db import models


class AgentConfig(models.Model):
    name = models.CharField(max_length=100, unique=True, default='default')
    llm_model = models.CharField(max_length=120, default='llama3.1:8b')
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.20)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class GlobalSettings(models.Model):
    name = models.CharField(max_length=100, unique=True, default='default')
    github_api_key = models.CharField(max_length=255, blank=True)
    problem99_api_key = models.CharField(max_length=255, blank=True)
    ollama_base_url = models.URLField(default='http://localhost:11434')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


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
    processed_issues = models.PositiveIntegerField(default=0)
    matched_issues = models.PositiveIntegerField(default=0)
    uploaded_issues = models.PositiveIntegerField(default=0)
    skipped_issues = models.PositiveIntegerField(default=0)
    error_issues = models.PositiveIntegerField(default=0)
    current_phase = models.CharField(max_length=60, blank=True)
    current_issue = models.PositiveIntegerField(null=True, blank=True)
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


class ScanStepLog(models.Model):
    """Granular step-by-step debug log for each phase of a scan task."""

    class Phase(models.TextChoices):
        SCAN_START = 'scan_start', 'Scan started'
        FETCH_ISSUES_PAGE = 'fetch_issues_page', 'Fetching issues page'
        FETCH_ISSUES_DONE = 'fetch_issues_done', 'Issues fetched'
        ISSUE_START = 'issue_start', 'Processing issue'
        FETCH_COMMENTS = 'fetch_comments', 'Fetching comments'
        FETCH_TIMELINE = 'fetch_timeline', 'Fetching timeline'
        FETCH_PR = 'fetch_pr', 'Fetching PR details'
        FETCH_PR_FILES = 'fetch_pr_files', 'Fetching PR files'
        AI_QUEUED = 'ai_queued', 'AI analysis queued'
        AI_RUNNING = 'ai_running', 'AI analysis running'
        AI_DONE = 'ai_done', 'AI analysis complete'
        AI_ERROR = 'ai_error', 'AI analysis error'
        QUALITY_GATE = 'quality_gate', 'Quality gate check'
        UPLOAD_START = 'upload_start', 'Problem99 upload started'
        UPLOAD_OK = 'upload_ok', 'Problem99 upload succeeded'
        UPLOAD_SKIP = 'upload_skip', 'Problem99 upload skipped'
        UPLOAD_FAIL = 'upload_fail', 'Problem99 upload failed'
        ISSUE_DONE = 'issue_done', 'Issue processing done'
        SCAN_DONE = 'scan_done', 'Scan completed'
        SCAN_ERROR = 'scan_error', 'Scan error'
        SCAN_STOPPED = 'scan_stopped', 'Scan stopped'

    class Level(models.TextChoices):
        DEBUG = 'debug', 'Debug'
        INFO = 'info', 'Info'
        WARNING = 'warning', 'Warning'
        ERROR = 'error', 'Error'

    scan_task = models.ForeignKey(ScanTask, on_delete=models.CASCADE, related_name='step_logs')
    phase = models.CharField(max_length=30, choices=Phase.choices)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    issue_number = models.PositiveIntegerField(null=True, blank=True)
    message = models.TextField()
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['scan_task', 'created_at']),
        ]

    def __str__(self):
        issue_part = f" issue #{self.issue_number}" if self.issue_number else ""
        return f"Scan #{self.scan_task_id}{issue_part} [{self.phase}] {self.message[:60]}"
