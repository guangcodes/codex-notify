"""Shared constants for the notification service."""

APP_NAME = "codex-notify"
KEYCHAIN_ACCOUNT = "codex-notify"
KEYCHAIN_CREDENTIALS_SERVICE = "codex-notify-feishu-credentials-v1"
KEYCHAIN_WEBHOOK_SERVICE = "codex-notify-feishu-webhook"
KEYCHAIN_SECRET_SERVICE = "codex-notify-feishu-signing-secret"
HOOK_STATUS_START = "Queueing Codex turn start notification"
PENDING_CONFIRMATION_SECONDS = 5
OUTBOX_RETENTION_SECONDS = 24 * 60 * 60
CLAIM_LEASE_SECONDS = 60
DEFAULT_BATCH_SIZE = 20
RETRY_DELAYS_SECONDS = (10, 30, 120, 600, 1800)
