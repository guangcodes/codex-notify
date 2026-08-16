"""Install hooks, a self-contained runtime, and a macOS LaunchAgent."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .computer_use import (
    COMPUTER_USE_BUNDLE_ID,
    COMPUTER_USE_TEAM_ID,
    PREVIOUS_NOTIFY_FLAG,
    ComputerUseIntegration,
    decode_previous_notify,
    inspect_computer_use,
)
from .constants import HOOK_STATUS_PERMISSION, HOOK_STATUS_START
from .paths import AppPaths


LAUNCH_AGENT_LABEL = "io.github.guangcodes.codex-notify"
LEGACY_LAUNCH_AGENT_LABEL = "com.guang.codex-turn-notifier"
CONFIG_BEGIN = "# >>> codex-notify managed notification >>>"
CONFIG_END = "# <<< codex-notify managed notification <<<"
CLI_MARKER = "# Managed by codex-notify. Do not edit."
INSTALL_STATE_VERSION = 2
LEGACY_INSTALL_STATE_VERSION = 1
PENDING_INSTALL_STATE_VERSION = 2
LEGACY_PENDING_INSTALL_STATE_VERSION = 1
_UNSET = object()
UNINSTALLED_RUNNER_MARKER = "# codex-notify uninstalled safety runner"
UNINSTALLED_RUNNER_CONTENT = (
    f"{UNINSTALLED_RUNNER_MARKER}\n"
    "# Kept so a stale Computer Use notification chain never points to a missing file.\n"
    "raise SystemExit(0)\n"
)

CURRENT_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "PermissionRequest",
)
LEGACY_HOOK_EVENTS = ("PreToolUse", "Stop")
OWNED_HOOK_EVENTS = CURRENT_HOOK_EVENTS + LEGACY_HOOK_EVENTS
LEGACY_STOP_STATUS_MESSAGE = "Queueing Codex turn completion notification"


@dataclass(frozen=True)
class _ConfigInstallPlan:
    current_document: str
    editable_document: str
    integration: ComputerUseIntegration
    previous_notify: tuple[str, ...]
    installed_notify: tuple[str, ...]
    original_notify: tuple[str, ...]
    original_notify_source: str


@dataclass(frozen=True)
class _ConfigUninstallPlan:
    current_document: str | None
    updated_document: str | None


@dataclass(frozen=True)
class _OwnershipSnapshot:
    """Preflight ownership evidence for one lifecycle-managed resource."""

    resource: str
    state: str
    path: Path | None = None


@dataclass
class _RollbackEntry:
    step: str
    resource: str
    transition: tuple[str, str]
    compensate: Callable[[], list[str]]
    barrier: bool = False


class _RollbackJournal:
    """One reverse-order compensation journal shared by install and uninstall."""

    def __init__(
        self,
        *,
        operation: str,
        checkpoint: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._entries: list[_RollbackEntry] = []
        self._operation = operation
        self._checkpoint = checkpoint

    def record(
        self,
        *,
        step: str,
        resource: str,
        transition: tuple[str, str],
        compensate: Callable[[], list[str]],
        barrier: bool = False,
    ) -> None:
        allowed = RESOURCE_ALLOWED_TRANSITIONS.get(resource, frozenset())
        if transition not in allowed:
            raise RuntimeError(
                f"事务步骤 {step} 包含未授权状态转换："
                f"{resource} {transition[0]} -> {transition[1]}"
            )
        self._entries.append(
            _RollbackEntry(step, resource, transition, compensate, barrier)
        )

    def rollback(self) -> list[str]:
        errors: list[str] = []
        for entry in reversed(self._entries):
            try:
                if self._checkpoint is not None:
                    self._checkpoint(
                        self._operation,
                        entry.step,
                        "rollback-before",
                    )
                entry_errors = entry.compensate()
            except BaseException as exc:
                entry_errors = [f"{entry.step} 回滚失败：{exc}"]
            errors.extend(entry_errors)
            if entry_errors and entry.barrier:
                break
        return errors


RESOURCE_ALLOWED_TRANSITIONS: dict[str, frozenset[tuple[str, str]]] = {
    "cli": frozenset({("absent", "absent"), ("owned", "absent"), ("foreign", "foreign")}),
    "current-launch-agent": frozenset(
        {
            ("absent", "owned"),
            ("owned", "owned"),
            ("owned", "absent"),
            ("absent", "absent"),
        }
    ),
    "legacy-launch-agent": frozenset(
        {("absent", "absent"), ("owned", "absent")}
    ),
    "hooks": frozenset(
        {("absent", "managed"), ("shared", "managed"), ("managed", "shared")}
    ),
    "notify": frozenset(
        {
            ("computer-use", "managed"),
            ("managed", "managed"),
            ("managed", "computer-use"),
            ("external", "external"),
        }
    ),
    "runtime": frozenset(
        {
            ("absent", "owned"),
            ("owned", "owned"),
            ("owned", "absent"),
            ("absent", "absent"),
        }
    ),
    "current-service": frozenset(
        {("loaded", "unloaded"), ("unloaded", "loaded")}
    ),
    "legacy-service": frozenset(
        {("loaded", "unloaded"), ("unloaded", "loaded")}
    ),
}


INSTALL_TRANSACTION_STEPS = (
    "stop-current-service",
    "stop-legacy-service",
    "prepare-runtime-directories",
    "publish-runtime",
    "publish-runner",
    "remove-legacy-cli",
    "remove-legacy-launch-agent",
    "publish-notify",
    "publish-pending-install-state",
    "publish-notify-config",
    "publish-install-state",
    "remove-pending-install-state",
    "publish-hooks",
    "publish-current-launch-agent",
    "start-current-service",
)

UNINSTALL_TRANSACTION_STEPS = (
    "stop-current-service",
    "stop-legacy-service",
    "remove-legacy-cli",
    "remove-legacy-launch-agent",
    "restore-notify",
    "remove-hooks",
    "remove-current-launch-agent",
    "publish-safety-runner",
    "remove-install-state",
    "remove-pending-install-state",
    "remove-runner",
    "remove-runtime",
    "purge-data",
    "purge-logs",
    "remove-runtime-root",
)


class _PublishedConfigError(RuntimeError):
    """The new config is visible and must participate in install rollback."""


class _PreserveTemporaryConfigError(_PublishedConfigError):
    """Recovery failed; the displaced config remains in the temporary path."""


def _stable_python_executable() -> Path:
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        candidate = os.environ.get("CONDA_PYTHON_EXE")
        if not candidate:
            raise RuntimeError("Conda 环境中找不到稳定的基础 Python 解释器")
    else:
        candidate = (
            getattr(sys, "_base_executable", None)
            if sys.prefix != sys.base_prefix
            else sys.executable
        )
    executable = Path(candidate or sys.executable).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise RuntimeError(f"找不到稳定的 Python 解释器：{executable}")
    _require_supported_python(executable)
    return executable


def _require_supported_python(executable: Path) -> None:
    if executable == Path(sys.executable):
        version = (sys.version_info.major, sys.version_info.minor)
    else:
        try:
            result = subprocess.run(
                [
                    str(executable),
                    "-c",
                    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"无法验证 Python 解释器：{executable}") from exc
        try:
            major, minor = (int(part) for part in result.stdout.strip().split(".", 1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"无法验证 Python 解释器：{executable}") from exc
        if result.returncode != 0:
            raise RuntimeError(f"无法验证 Python 解释器：{executable}")
        version = (major, minor)
    if version < (3, 11):
        raise RuntimeError(f"稳定解释器必须是 Python 3.11 或更高版本：{executable}")


class Installer:
    def __init__(self, package_dir: Path, *, home: Path | None = None):
        resolved_package = package_dir.resolve()
        legacy_source_package = resolved_package / "src" / "codex_notify"
        # Keep source-checkout callers from pre-v0.1 upgrades working while all
        # public entry points now pass the package directory explicitly.
        self.package_dir = (
            legacy_source_package
            if legacy_source_package.is_dir()
            else resolved_package
        )
        self.home = (home or Path.home()).resolve()
        self.paths = AppPaths(self.home / ".codex" / "codex-notify")
        self._python_executable: Path | None = None
        self._transaction_backup_files: list[Path] | None = None
        self._transaction_after_images: dict[str, bytes] | None = None
        self._config_install_plan: _ConfigInstallPlan | None = None
        self._validated_managed_hooks: dict[str, list[dict[str, Any]]] = {}
        self._validated_hooks_content: bytes | None = None
        self._validated_hooks_snapshot = False
        self._ownership_snapshots: dict[str, _OwnershipSnapshot] = {}

    @property
    def python_executable(self) -> Path:
        if self._python_executable is None:
            self._python_executable = _stable_python_executable()
        return self._python_executable

    @property
    def hooks_file(self) -> Path:
        return self.home / ".codex" / "hooks.json"

    @property
    def launch_agent(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    @property
    def legacy_launch_agent(self) -> Path:
        return (
            self.home
            / "Library"
            / "LaunchAgents"
            / f"{LEGACY_LAUNCH_AGENT_LABEL}.plist"
        )

    @property
    def cli_path(self) -> Path:
        return self.home / ".local" / "bin" / "codex-notify"

    @property
    def config_file(self) -> Path:
        return self.home / ".codex" / "config.toml"

    @property
    def computer_use_executable(self) -> Path:
        return (
            self.home
            / ".codex"
            / "computer-use"
            / "Codex Computer Use.app"
            / "Contents"
            / "SharedSupport"
            / "SkyComputerUseClient.app"
            / "Contents"
            / "MacOS"
            / "SkyComputerUseClient"
        )

    def _transaction_checkpoint(self, operation: str, step: str, phase: str) -> None:
        """Stable fault-injection seam; production execution is intentionally a no-op."""

    def _run_transaction_step(
        self,
        operation: str,
        step: str,
        action: Callable[[], Any],
        *,
        on_applied: Callable[[Any], None] | None = None,
    ) -> Any:
        declared = (
            INSTALL_TRANSACTION_STEPS
            if operation == "install"
            else UNINSTALL_TRANSACTION_STEPS
        )
        if step not in declared:
            raise RuntimeError(f"未声明的 {operation} 事务步骤：{step}")
        self._transaction_checkpoint(operation, step, "before")
        result = action()
        if on_applied is not None:
            on_applied(result)
        self._transaction_checkpoint(operation, step, "after")
        return result

    def _capture_ownership_snapshots(
        self,
        *,
        launch_agent_content: bytes | None,
        legacy_launch_agent_content: bytes | None,
        legacy_cli_content: bytes | None,
        hooks_content: bytes | None,
        notify_state: str,
    ) -> dict[str, _OwnershipSnapshot]:
        cli_state = (
            "owned"
            if legacy_cli_content is not None
            else (
                "foreign"
                if self.cli_path.exists() or self.cli_path.is_symlink()
                else "absent"
            )
        )
        snapshots = {
            "cli": _OwnershipSnapshot("cli", cli_state, self.cli_path),
            "current-launch-agent": _OwnershipSnapshot(
                "current-launch-agent",
                "owned" if launch_agent_content is not None else "absent",
                self.launch_agent,
            ),
            "legacy-launch-agent": _OwnershipSnapshot(
                "legacy-launch-agent",
                "owned" if legacy_launch_agent_content is not None else "absent",
                self.legacy_launch_agent,
            ),
            "hooks": _OwnershipSnapshot(
                "hooks",
                "shared" if hooks_content is not None else "absent",
                self.hooks_file,
            ),
            "notify": _OwnershipSnapshot("notify", notify_state, self.config_file),
            "runtime": _OwnershipSnapshot(
                "runtime",
                "owned" if self.paths.library_dir.exists() else "absent",
                self.paths.library_dir,
            ),
        }
        self._ownership_snapshots = snapshots
        return snapshots

    def install(self, *, start_agent: bool = True) -> None:
        lock_descriptor = self._acquire_operation_lock()
        try:
            self._install_locked(start_agent=start_agent)
        finally:
            self._release_operation_lock(lock_descriptor)

    def _install_locked(self, *, start_agent: bool) -> None:
        self._validate_runtime_directory_targets()
        self._recover_pending_install_state()
        hooks_document, hooks_content = self._read_hooks_snapshot()
        self._validate_hooks_document(hooks_document)
        self._validated_hooks_content = hooks_content
        self._validated_hooks_snapshot = True
        self._validate_source()
        self._validate_owned_install_targets()
        launch_agent_content = self._validate_launch_agent()
        legacy_cli_content = self._legacy_cli_migration_content()
        remove_legacy_cli = legacy_cli_content is not None
        legacy_launch_agent_content = self._validate_legacy_launch_agent()
        # Installation needs a durable interpreter, but uninstall does not.
        # Resolve it before the first installation write so an unavailable or
        # unsupported interpreter cannot leave a partial runtime behind.
        _ = self.python_executable
        self._config_install_plan = self._prepare_config_install()
        self._capture_ownership_snapshots(
            launch_agent_content=launch_agent_content,
            legacy_launch_agent_content=legacy_launch_agent_content,
            legacy_cli_content=legacy_cli_content,
            hooks_content=hooks_content,
            notify_state=(
                "computer-use"
                if self._config_install_plan.integration.previous_notify is None
                else "managed"
            ),
        )
        runtime_directories = self._snapshot_runtime_directories()
        agent_was_loaded = self._is_launch_agent_loaded()
        legacy_agent_was_loaded = self._is_legacy_launch_agent_loaded()
        if agent_was_loaded and launch_agent_content is None:
            raise ValueError(
                "LaunchAgent 服务仍在运行但 plist 缺失，无法确认所有权或安全恢复"
            )
        if legacy_agent_was_loaded and not self.legacy_launch_agent.exists():
            raise ValueError(
                "旧 LaunchAgent 服务仍在运行但 plist 缺失，无法确认所有权或安全恢复"
            )
        backup_root, snapshots = self._snapshot_install_targets(
            include_legacy_cli=remove_legacy_cli,
            include_legacy_launch_agent=legacy_launch_agent_content is not None,
        )
        self._transaction_backup_files = []
        self._transaction_after_images = {}
        agent_stopped_for_install = False
        legacy_agent_stopped_for_install = False
        installation_writes_started = False
        try:
            if agent_was_loaded:
                agent_stopped_for_install = True
                try:
                    self._run_transaction_step(
                        "install",
                        "stop-current-service",
                        self._bootout_launch_agent,
                    )
                except BaseException:
                    try:
                        agent_stopped_for_install = not self._is_launch_agent_loaded()
                    except Exception:
                        pass
                    raise
            if legacy_agent_was_loaded:
                legacy_agent_stopped_for_install = True
                try:
                    self._run_transaction_step(
                        "install",
                        "stop-legacy-service",
                        self._bootout_legacy_launch_agent,
                    )
                except BaseException:
                    try:
                        legacy_agent_stopped_for_install = (
                            not self._is_legacy_launch_agent_loaded()
                        )
                    except Exception:
                        pass
                    raise
            self._assert_migration_file_unchanged(
                self.launch_agent, launch_agent_content, "LaunchAgent"
            )
            if launch_agent_content is None:
                self._assert_migration_path_absent(self.launch_agent, "LaunchAgent")
            self._assert_migration_file_unchanged(
                self.cli_path, legacy_cli_content, "旧 codex-notify CLI"
            )
            self._assert_migration_file_unchanged(
                self.legacy_launch_agent,
                legacy_launch_agent_content,
                "旧 LaunchAgent",
            )
            if legacy_launch_agent_content is None:
                self._assert_migration_path_absent(
                    self.legacy_launch_agent, "旧 LaunchAgent"
                )
            installation_writes_started = True
            self._run_transaction_step(
                "install",
                "prepare-runtime-directories",
                self.paths.ensure_runtime_dirs,
            )
            self._run_transaction_step(
                "install", "publish-runtime", self._install_library
            )
            self._run_transaction_step(
                "install", "publish-runner", self._install_runner
            )
            if remove_legacy_cli:
                self._run_transaction_step(
                    "install",
                    "remove-legacy-cli",
                    lambda: _remove_file_if_unchanged(
                        self.cli_path,
                        expected_content=legacy_cli_content,
                    ),
                )
            if legacy_launch_agent_content is not None:
                if self._is_legacy_launch_agent_loaded():
                    legacy_agent_was_loaded = True
                    legacy_agent_stopped_for_install = True
                    try:
                        self._run_transaction_step(
                            "install",
                            "stop-legacy-service",
                            self._bootout_legacy_launch_agent,
                        )
                    except BaseException:
                        try:
                            legacy_agent_stopped_for_install = (
                                not self._is_legacy_launch_agent_loaded()
                            )
                        except Exception:
                            pass
                        raise
                self._run_transaction_step(
                    "install",
                    "remove-legacy-launch-agent",
                    lambda: _remove_file_if_unchanged(
                        self.legacy_launch_agent,
                        expected_content=legacy_launch_agent_content,
                    ),
                )
            self._run_transaction_step(
                "install",
                "publish-notify",
                lambda: self._install_config_notify(self._config_install_plan),
            )
            self._run_transaction_step(
                "install", "publish-hooks", self._install_hooks
            )
            if self._is_launch_agent_loaded():
                agent_was_loaded = True
                agent_stopped_for_install = True
                try:
                    self._run_transaction_step(
                        "install",
                        "stop-current-service",
                        self._bootout_launch_agent,
                    )
                except BaseException:
                    try:
                        agent_stopped_for_install = not self._is_launch_agent_loaded()
                    except Exception:
                        pass
                    raise
            self._run_transaction_step(
                "install",
                "publish-current-launch-agent",
                lambda: self._install_launch_agent(
                    expected_content=launch_agent_content
                ),
            )
            self._assert_migration_path_absent(
                self.legacy_launch_agent, "旧 LaunchAgent"
            )
            if start_agent:
                if agent_stopped_for_install:
                    self._run_transaction_step(
                        "install",
                        "start-current-service",
                        self._bootstrap_launch_agent,
                    )
                else:
                    self._run_transaction_step(
                        "install",
                        "start-current-service",
                        self._reload_launch_agent,
                    )
        except BaseException as exc:
            journal = _RollbackJournal(
                operation="install",
                checkpoint=self._transaction_checkpoint,
            )

            def restore_legacy_service() -> list[str]:
                try:
                    if legacy_launch_agent_content is None:
                        raise RuntimeError("缺少旧 LaunchAgent 的预检快照")
                    self._assert_migration_file_unchanged(
                        self.legacy_launch_agent,
                        legacy_launch_agent_content,
                        "旧 LaunchAgent",
                    )
                    self._bootstrap_legacy_launch_agent()
                    return []
                except Exception as rollback_exc:
                    return [str(rollback_exc)]

            def restore_current_service() -> list[str]:
                try:
                    if launch_agent_content is None:
                        raise RuntimeError("缺少 LaunchAgent 的预检快照")
                    self._assert_migration_file_unchanged(
                        self.launch_agent,
                        launch_agent_content,
                        "LaunchAgent",
                    )
                    self._bootstrap_launch_agent()
                    return []
                except Exception as rollback_exc:
                    return [str(rollback_exc)]

            def rollback_filesystem() -> list[str]:
                errors = self._rollback_install(backup_root, snapshots)
                errors.extend(
                    self._restore_runtime_directories(runtime_directories)
                )
                for backup in self._transaction_backup_files or []:
                    backup.unlink(missing_ok=True)
                return errors

            def stop_new_service() -> list[str]:
                try:
                    self._bootout_launch_agent()
                    return []
                except Exception as rollback_exc:
                    return [str(rollback_exc)]

            if legacy_agent_stopped_for_install:
                journal.record(
                    step="stop-legacy-service",
                    resource="legacy-service",
                    transition=("loaded", "unloaded"),
                    compensate=restore_legacy_service,
                )
            if agent_was_loaded and agent_stopped_for_install:
                journal.record(
                    step="stop-current-service",
                    resource="current-service",
                    transition=("loaded", "unloaded"),
                    compensate=restore_current_service,
                )
            if installation_writes_started:
                journal.record(
                    step="filesystem-publication",
                    resource="runtime",
                    transition=(
                        self._ownership_snapshots["runtime"].state,
                        "owned",
                    ),
                    compensate=rollback_filesystem,
                    barrier=True,
                )
            else:
                shutil.rmtree(backup_root, ignore_errors=True)
            if start_agent:
                journal.record(
                    step="start-current-service",
                    resource="current-service",
                    transition=("unloaded", "loaded"),
                    compensate=stop_new_service,
                )
            rollback_errors = journal.rollback()
            if rollback_errors:
                raise RuntimeError(
                    f"安装失败且回滚不完整：{exc}；{'；'.join(rollback_errors)}"
                ) from exc
            raise
        else:
            shutil.rmtree(backup_root, ignore_errors=True)
            for backup in self._transaction_backup_files or []:
                backup.unlink(missing_ok=True)
        finally:
            self._transaction_backup_files = None
            self._transaction_after_images = None
            self._config_install_plan = None

    def uninstall(self, *, purge: bool = False) -> None:
        lock_descriptor = self._acquire_operation_lock()
        try:
            self._uninstall_locked(purge=purge)
        finally:
            self._release_operation_lock(lock_descriptor)

    def _uninstall_locked(self, *, purge: bool) -> None:
        self._validate_runtime_directory_targets()
        self._recover_pending_install_state()
        launch_agent_content = self._validate_launch_agent()
        legacy_cli_content = self._legacy_cli_migration_content()
        retain_safety_runner, remove_runner = self._prepare_runner_uninstall()
        config_action = self._prepare_config_uninstall()
        hooks_document, hooks_content = self._read_hooks_snapshot()
        self._validate_hooks_document(hooks_document)
        self._validated_hooks_content = hooks_content
        self._validated_hooks_snapshot = True
        legacy_launch_agent_content = self._validate_legacy_launch_agent()
        notify_state = "external"
        if config_action.current_document is not None:
            try:
                current_notify = tomllib.loads(
                    config_action.current_document
                ).get("notify")
            except tomllib.TOMLDecodeError:
                current_notify = None
            if _notify_references_runner(current_notify, self.paths.runner):
                notify_state = "managed"
            elif config_action.updated_document is not None:
                notify_state = "managed"
        self._capture_ownership_snapshots(
            launch_agent_content=launch_agent_content,
            legacy_launch_agent_content=legacy_launch_agent_content,
            legacy_cli_content=legacy_cli_content,
            hooks_content=hooks_content,
            notify_state=notify_state,
        )
        agent_was_loaded = self._is_launch_agent_loaded()
        legacy_agent_was_loaded = self._is_legacy_launch_agent_loaded()
        if agent_was_loaded and launch_agent_content is None:
            raise ValueError(
                "LaunchAgent 服务仍在运行但 plist 缺失，无法确认所有权或安全恢复"
            )
        if legacy_agent_was_loaded and not self.legacy_launch_agent.exists():
            raise ValueError(
                "旧 LaunchAgent 服务仍在运行但 plist 缺失，无法确认所有权或安全恢复"
            )
        backup_root, snapshots = self._snapshot_install_targets(
            include_legacy_cli=legacy_cli_content is not None,
            include_legacy_launch_agent=legacy_launch_agent_content is not None,
            include_runtime_data=purge,
        )
        runtime_directories = self._snapshot_runtime_directories()
        agent_stopped_for_uninstall = False
        current_agent_stop_confirmed = False
        legacy_agent_stopped_for_uninstall = False
        legacy_cli_removed = False
        legacy_launch_agent_removed = False
        removed_hooks_content: bytes | None = None

        def mark_legacy_cli_removed(_result: Any) -> None:
            nonlocal legacy_cli_removed
            legacy_cli_removed = True

        def mark_legacy_launch_agent_removed(_result: Any) -> None:
            nonlocal legacy_launch_agent_removed
            legacy_launch_agent_removed = True

        def capture_removed_hooks(content: Any) -> None:
            nonlocal removed_hooks_content
            removed_hooks_content = content

        def capture_current_agent_stop(stopped: Any) -> None:
            nonlocal agent_stopped_for_uninstall, current_agent_stop_confirmed
            if stopped is True:
                current_agent_stop_confirmed = True
                agent_stopped_for_uninstall = True

        try:
            agent_stopped_for_uninstall = agent_was_loaded
            try:
                self._run_transaction_step(
                    "uninstall",
                    "stop-current-service",
                    self._bootout_launch_agent,
                    on_applied=capture_current_agent_stop,
                )
            except BaseException:
                if not current_agent_stop_confirmed:
                    try:
                        agent_stopped_for_uninstall = (
                            agent_was_loaded
                            and not self._is_launch_agent_loaded()
                        )
                    except Exception:
                        pass
                raise
            if legacy_agent_was_loaded:
                legacy_agent_stopped_for_uninstall = True
                try:
                    self._run_transaction_step(
                        "uninstall",
                        "stop-legacy-service",
                        self._bootout_legacy_launch_agent,
                    )
                except BaseException:
                    try:
                        legacy_agent_stopped_for_uninstall = (
                            not self._is_legacy_launch_agent_loaded()
                        )
                    except Exception:
                        pass
                    raise
            self._assert_migration_file_unchanged(
                self.launch_agent, launch_agent_content, "LaunchAgent"
            )
            if launch_agent_content is None:
                self._assert_migration_path_absent(self.launch_agent, "LaunchAgent")
            self._assert_migration_file_unchanged(
                self.cli_path, legacy_cli_content, "旧 codex-notify CLI"
            )
            self._assert_migration_file_unchanged(
                self.legacy_launch_agent,
                legacy_launch_agent_content,
                "旧 LaunchAgent",
            )
            if legacy_cli_content is not None:
                self._run_transaction_step(
                    "uninstall",
                    "remove-legacy-cli",
                    lambda: _remove_file_if_unchanged(
                        self.cli_path,
                        expected_content=legacy_cli_content,
                    ),
                    on_applied=mark_legacy_cli_removed,
                )
            if legacy_launch_agent_content is not None:
                if self._is_legacy_launch_agent_loaded():
                    legacy_agent_was_loaded = True
                    legacy_agent_stopped_for_uninstall = True
                    try:
                        self._run_transaction_step(
                            "uninstall",
                            "stop-legacy-service",
                            self._bootout_legacy_launch_agent,
                        )
                    except BaseException:
                        try:
                            legacy_agent_stopped_for_uninstall = (
                                not self._is_legacy_launch_agent_loaded()
                            )
                        except Exception:
                            pass
                        raise
                self._run_transaction_step(
                    "uninstall",
                    "remove-legacy-launch-agent",
                    lambda: _remove_file_if_unchanged(
                        self.legacy_launch_agent,
                        expected_content=legacy_launch_agent_content,
                    ),
                    on_applied=mark_legacy_launch_agent_removed,
                )
            self._run_transaction_step(
                "uninstall",
                "restore-notify",
                lambda: self._apply_config_uninstall(config_action),
            )
            self._assert_config_does_not_reference_runner()
            removed_hooks_content = self._run_transaction_step(
                "uninstall",
                "remove-hooks",
                self._remove_hooks,
                on_applied=capture_removed_hooks,
            )
            if launch_agent_content is not None:
                self._run_transaction_step(
                    "uninstall",
                    "remove-current-launch-agent",
                    lambda: _remove_file_if_unchanged(
                        self.launch_agent,
                        expected_content=launch_agent_content,
                    ),
                )
            else:
                self._assert_migration_path_absent(self.launch_agent, "LaunchAgent")
        except BaseException as exc:
            restore_errors = self._rollback_uninstall_transaction(
                snapshots=snapshots,
                runtime_directories=runtime_directories,
                config_action=config_action,
                hooks_content=hooks_content,
                removed_hooks_content=removed_hooks_content,
                launch_agent_content=launch_agent_content,
                legacy_cli_content=(
                    legacy_cli_content if legacy_cli_removed else None
                ),
                legacy_launch_agent_content=(
                    legacy_launch_agent_content
                    if legacy_launch_agent_removed
                    else None
                ),
                agent_stopped=agent_stopped_for_uninstall,
                legacy_agent_stopped=legacy_agent_stopped_for_uninstall,
                retain_safety_runner=False,
                remove_runner=False,
                purge=False,
            )
            if not restore_errors:
                shutil.rmtree(backup_root, ignore_errors=True)
            if restore_errors:
                raise RuntimeError(
                    f"卸载失败且回滚不完整：{exc}；{'；'.join(restore_errors)}"
                ) from exc
            raise
        try:
            if retain_safety_runner:
                self._run_transaction_step(
                    "uninstall",
                    "publish-safety-runner",
                    self._install_uninstalled_safety_runner,
                )
            self._run_transaction_step(
                "uninstall",
                "remove-install-state",
                lambda: self.paths.install_state.unlink(missing_ok=True),
            )
            self._run_transaction_step(
                "uninstall",
                "remove-pending-install-state",
                lambda: self.paths.pending_install_state.unlink(missing_ok=True),
            )
            if remove_runner:
                self._run_transaction_step(
                    "uninstall",
                    "remove-runner",
                    lambda: self.paths.runner.unlink(missing_ok=True),
                )
            if self.paths.library_dir.exists():
                self._run_transaction_step(
                    "uninstall",
                    "remove-runtime",
                    lambda: self._remove_install_target(self.paths.library_dir),
                )
            if purge:
                for step, directory in (
                    ("purge-data", self.paths.data_dir),
                    ("purge-logs", self.paths.log_dir),
                ):
                    if directory.exists():
                        self._run_transaction_step(
                            "uninstall",
                            step,
                            lambda directory=directory: self._remove_install_target(
                                directory
                            ),
                        )
                if not retain_safety_runner and self.paths.root.exists():
                    try:
                        next(self.paths.root.iterdir())
                    except StopIteration:
                        self._run_transaction_step(
                            "uninstall",
                            "remove-runtime-root",
                            self.paths.root.rmdir,
                        )
        except BaseException as exc:
            restore_errors = self._rollback_uninstall_transaction(
                snapshots=snapshots,
                runtime_directories=runtime_directories,
                config_action=config_action,
                hooks_content=hooks_content,
                removed_hooks_content=removed_hooks_content,
                launch_agent_content=launch_agent_content,
                legacy_cli_content=legacy_cli_content,
                legacy_launch_agent_content=legacy_launch_agent_content,
                agent_stopped=agent_stopped_for_uninstall,
                legacy_agent_stopped=legacy_agent_stopped_for_uninstall,
                retain_safety_runner=retain_safety_runner,
                remove_runner=remove_runner,
                purge=purge,
            )
            if restore_errors:
                raise RuntimeError(
                    f"卸载失败且回滚不完整：{exc}；{'；'.join(restore_errors)}"
                ) from exc
            shutil.rmtree(backup_root, ignore_errors=True)
            raise
        else:
            shutil.rmtree(backup_root, ignore_errors=True)

    @property
    def operation_lock_file(self) -> Path:
        return self.home / ".codex" / ".codex-notify-operation.lock"

    def _acquire_operation_lock(self) -> int:
        self.operation_lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.operation_lock_file, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("无法打开 codex-notify 安装事务锁") from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError("另一个 codex-notify 安装或卸载操作正在进行") from exc
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _release_operation_lock(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _install_library(self) -> None:
        source = self.package_dir
        self.paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = self.paths.root / f".lib-staging-{os.getpid()}-{time.time_ns()}"
        try:
            shutil.copytree(
                source,
                staging / "codex_notify",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            if self.paths.library_dir.exists():
                _swap_directories(staging, self.paths.library_dir)
            else:
                staging.replace(self.paths.library_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _install_runner(self) -> None:
        content = f"""#!{self.python_executable}
import os
import sys
os.environ["CODEX_NOTIFY_HOME"] = {str(self.paths.root)!r}
sys.path.insert(0, {str(self.paths.library_dir)!r})
from codex_notify.cli import main
raise SystemExit(main())
"""
        self._atomic_write_text(
            self.paths.runner,
            content,
            default_mode=0o700,
            follow_symlinks=False,
        )

    def _install_uninstalled_safety_runner(self) -> None:
        self._atomic_write_text(
            self.paths.runner,
            UNINSTALLED_RUNNER_CONTENT,
            default_mode=0o700,
            follow_symlinks=False,
        )

    def _prepare_runner_uninstall(self) -> tuple[bool, bool]:
        state_exists = self.paths.install_state.exists()
        runner = self.paths.runner
        if runner.is_symlink():
            if state_exists:
                raise ValueError(
                    f"runner 路径是符号链接，卸载器不会覆盖：{runner}"
                )
            return False, False
        if not runner.exists():
            return state_exists, False
        if not runner.is_file():
            raise ValueError(f"runner 路径不是普通文件，卸载器不会操作：{runner}")
        try:
            content = runner.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"无法确认 runner 所有权：{runner}") from exc
        is_safety_runner = content == UNINSTALLED_RUNNER_CONTENT
        is_managed_runner = _is_managed_runner_content(
            content,
            root=self.paths.root,
            library_dir=self.paths.library_dir,
        )
        if state_exists:
            if not is_safety_runner and not is_managed_runner:
                raise ValueError(
                    f"runner 内容不属于 codex-notify，卸载器不会覆盖：{runner}"
                )
            return True, False
        if is_safety_runner:
            return True, False
        return False, is_managed_runner

    def _has_uninstalled_safety_runner(self) -> bool:
        runner = self.paths.runner
        if runner.is_symlink() or not runner.is_file():
            return False
        try:
            return runner.read_text(encoding="utf-8") == UNINSTALLED_RUNNER_CONTENT
        except OSError as exc:
            raise ValueError(f"无法确认安全 runner 所有权：{runner}") from exc

    def _legacy_cli_migration_content(self) -> bytes | None:
        """Return exact legacy shim bytes when installer ownership is confirmed."""
        if self.cli_path.is_symlink() or not self.cli_path.is_file():
            return None
        try:
            content = self.cli_path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"无法确认旧 codex-notify CLI 所有权：{self.cli_path}"
            ) from exc
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if _is_managed_cli_content(decoded, runner=self.paths.runner):
            return content
        return None

    def _prepare_legacy_cli_migration(self) -> bool:
        """Return whether an exact legacy installer-owned shim should be removed."""
        return self._legacy_cli_migration_content() is not None

    @staticmethod
    def _assert_migration_file_unchanged(
        path: Path, expected_content: bytes | None, label: str
    ) -> None:
        if expected_content is None:
            return
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"{label} 在预检后发生变化，已停止删除：{path}")
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"无法重新确认 {label}：{path}") from exc
        if current != expected_content:
            raise RuntimeError(f"{label} 在预检后发生变化，已停止删除：{path}")

    @staticmethod
    def _assert_migration_path_absent(path: Path, label: str) -> None:
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"{label} 在预检后出现，已停止安装：{path}")

    def _validate_owned_install_targets(self) -> None:
        targets = (
            ("runtime", self.paths.library_dir),
            ("runner", self.paths.runner),
            ("安装状态", self.paths.install_state),
            ("待恢复安装状态", self.paths.pending_install_state),
            ("LaunchAgent", self.launch_agent),
            ("旧 LaunchAgent", self.legacy_launch_agent),
        )
        for label, path in targets:
            if path.is_symlink():
                raise ValueError(
                    f"{label} 路径是符号链接，安装器不会覆盖：{path}"
                )

    def _validate_runtime_directory_targets(self) -> None:
        targets = (
            ("runtime root", self.paths.root),
            ("runtime data", self.paths.data_dir),
            ("runtime logs", self.paths.log_dir),
        )
        for label, path in targets:
            if path.is_symlink():
                raise ValueError(
                    f"{label} 路径是符号链接，安装器不会操作：{path}"
                )
            if path.exists() and not path.is_dir():
                raise ValueError(
                    f"{label} 路径不是目录，安装器不会操作：{path}"
                )

    def _hook_command(self, event_name: str) -> str:
        return " ".join(
            [
                shlex.quote(str(self.python_executable)),
                shlex.quote(str(self.paths.runner)),
                "hook",
                shlex.quote(event_name),
            ]
        )

    def _install_hooks(self) -> None:
        document, current_content = self._read_hooks_snapshot()
        self._assert_hooks_unchanged(current_content)
        hooks = document.setdefault("hooks", {})
        for event_name in CURRENT_HOOK_EVENTS:
            groups = _without_our_hook(
                hooks.get(event_name, []),
                runner=self.paths.runner,
                event_name=event_name,
                validated_items=self._validated_managed_hooks.get(event_name, []),
            )
            handler = {
                "type": "command",
                "command": self._hook_command(event_name),
                "timeout": 5,
            }
            if event_name == "UserPromptSubmit":
                handler["statusMessage"] = HOOK_STATUS_START
            elif event_name == "PermissionRequest":
                handler["statusMessage"] = HOOK_STATUS_PERMISSION
            group: dict[str, Any] = {"hooks": [handler]}
            if event_name == "PermissionRequest":
                group["matcher"] = ".*"
            groups.append(group)
            hooks[event_name] = groups

        for event_name in LEGACY_HOOK_EVENTS:
            legacy_groups = _without_our_hook(
                hooks.get(event_name, []),
                runner=self.paths.runner,
                event_name=event_name,
                validated_items=self._validated_managed_hooks.get(event_name, []),
            )
            if legacy_groups:
                hooks[event_name] = legacy_groups
            else:
                hooks.pop(event_name, None)
        self._write_hooks(document, expected_content=current_content)

    def _previous_notify(self) -> tuple[str, str, str]:
        return (
            str(self.python_executable),
            str(self.paths.runner),
            "notify",
        )

    def _prepare_config_install(self) -> _ConfigInstallPlan:
        if not self.config_file.exists():
            raise ValueError(
                "未找到 ~/.codex/config.toml 中的 Computer Use notify；"
                "请先安装并启用 Computer Use"
            )
        current = self.config_file.read_text(encoding="utf-8")
        try:
            config = tomllib.loads(current)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"现有 ~/.codex/config.toml 不是有效 TOML：{exc}") from exc
        if _hooks_explicitly_disabled(config):
            raise ValueError(
                "Codex Hooks 已在 ~/.codex/config.toml 中关闭；"
                "请先移除 features.hooks = false 再安装"
            )
        if "notify" not in config:
            raise ValueError(
                "未找到 Computer Use notify；请先安装并启用 Computer Use"
            )
        editable_document = current
        legacy_unmanaged = _remove_managed_config_block(current)
        if legacy_unmanaged != current and _is_legacy_managed_notify(
            config["notify"], runner=self.paths.runner
        ):
            try:
                unmanaged_config = tomllib.loads(legacy_unmanaged)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(
                    f"移除旧版 notify 后 config.toml 不是有效 TOML：{exc}"
                ) from exc
            if "notify" in unmanaged_config:
                raise ValueError("旧版配置之外仍存在 notify，安装器不会猜测合并")
            integration = inspect_computer_use(
                [str(self.computer_use_executable), "turn-ended"]
            )
            original_source = "notify = " + json.dumps(
                list(integration.notify), ensure_ascii=False
            ) + "\n"
            editable_document = original_source + legacy_unmanaged
        else:
            integration = inspect_computer_use(config["notify"])
            original_source = ""
        previous_notify = self._previous_notify()
        installed_notify = tuple(integration.chained_notify(previous_notify))
        if integration.previous_notify is None:
            if not original_source:
                _, _, original_source = _top_level_notify_assignment(current)
            return _ConfigInstallPlan(
                current_document=current,
                editable_document=editable_document,
                integration=integration,
                previous_notify=previous_notify,
                installed_notify=installed_notify,
                original_notify=integration.notify,
                original_notify_source=original_source,
            )
        if not self.paths.install_state.exists():
            if self._has_uninstalled_safety_runner() and _is_legacy_managed_notify(
                list(integration.previous_notify), runner=self.paths.runner
            ):
                original_notify = integration.base_notify
                original_source = "notify = " + json.dumps(
                    list(original_notify), ensure_ascii=False
                ) + "\n"
                return _ConfigInstallPlan(
                    current_document=current,
                    editable_document=editable_document,
                    integration=integration,
                    previous_notify=previous_notify,
                    installed_notify=installed_notify,
                    original_notify=original_notify,
                    original_notify_source=original_source,
                )
            if integration.previous_notify != previous_notify:
                raise ValueError(
                    "Computer Use 的 --previous-notify 已被其他命令占用；"
                    "安装器不会覆盖"
                )
            raise ValueError("缺少 codex-notify 安装状态，无法安全修改通知链")
        state = self._read_install_state(required=True)
        original_notify, original_source = _validate_install_state_for_integration(
            state, integration
        )
        return _ConfigInstallPlan(
            current_document=current,
            editable_document=editable_document,
            integration=integration,
            previous_notify=previous_notify,
            installed_notify=installed_notify,
            original_notify=original_notify,
            original_notify_source=original_source,
        )

    def _install_config_notify(self, plan: _ConfigInstallPlan) -> None:
        current = self.config_file.read_text(encoding="utf-8")
        if current != plan.current_document:
            raise RuntimeError("config.toml 在安装期间发生变化，已停止写入")
        replacement = "notify = " + json.dumps(
            list(plan.installed_notify), ensure_ascii=False
        ) + "\n"
        updated = _replace_top_level_notify(plan.editable_document, replacement)
        state = {
            "schema_version": INSTALL_STATE_VERSION,
            "runtime_version": __version__,
            "computer_use": {
                "executable": str(plan.integration.executable),
                "bundle_id": COMPUTER_USE_BUNDLE_ID,
                "team_id": COMPUTER_USE_TEAM_ID,
                "version": plan.integration.version,
                "signature_verified": plan.integration.signature_verified,
            },
            "original_notify": list(plan.original_notify),
            "original_notify_source": plan.original_notify_source,
            "installed_notify": list(plan.installed_notify),
            "previous_notify": list(plan.previous_notify),
        }
        pending = {
            "schema_version": PENDING_INSTALL_STATE_VERSION,
            "expected_notify": list(
                _string_tuple(
                    tomllib.loads(current).get("notify"),
                    "待恢复 expected_notify",
                )
            ),
            "state": state,
        }
        self._run_transaction_step(
            "install",
            "publish-pending-install-state",
            lambda: self._atomic_write_text(
                self.paths.pending_install_state,
                json.dumps(pending, ensure_ascii=False, indent=2) + "\n",
                default_mode=0o600,
                follow_symlinks=False,
            ),
        )
        if updated != current:
            self._run_transaction_step(
                "install",
                "publish-notify-config",
                lambda: self._write_config(updated, expected_content=current),
            )
        self._run_transaction_step(
            "install",
            "publish-install-state",
            lambda: self._write_install_state(state),
        )
        self._run_transaction_step(
            "install",
            "remove-pending-install-state",
            self.paths.pending_install_state.unlink,
        )

    def _write_install_state(self, state: dict[str, Any]) -> None:
        self._atomic_write_text(
            self.paths.install_state,
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            default_mode=0o600,
            follow_symlinks=False,
        )

    def _recover_pending_install_state(self) -> None:
        pending_path = self.paths.pending_install_state
        if not pending_path.exists():
            return
        if pending_path.is_symlink():
            raise ValueError("codex-notify 待恢复安装状态不能是符号链接")
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("codex-notify 待恢复安装状态无效") from exc
        if not isinstance(pending, dict) or (
            pending.get("schema_version")
            not in {
                PENDING_INSTALL_STATE_VERSION,
                LEGACY_PENDING_INSTALL_STATE_VERSION,
            }
            or set(pending) != {"schema_version", "expected_notify", "state"}
        ):
            raise ValueError("codex-notify 待恢复安装状态版本不受支持")
        expected_notify = _string_tuple(
            pending.get("expected_notify"), "待恢复 expected_notify"
        )
        state = pending.get("state")
        if not isinstance(state, dict) or state.get("schema_version") not in {
            INSTALL_STATE_VERSION,
            LEGACY_INSTALL_STATE_VERSION,
        }:
            raise ValueError("codex-notify 待恢复安装状态中的最终状态无效")
        if state.get("schema_version") == INSTALL_STATE_VERSION and (
            not isinstance(state.get("runtime_version"), str)
            or not state["runtime_version"]
        ):
            raise ValueError(
                "codex-notify 待恢复安装状态缺少 runtime_version"
            )
        installed_notify = _string_tuple(
            state.get("installed_notify"), "待恢复 installed_notify"
        )
        _string_tuple(state.get("previous_notify"), "待恢复 previous_notify")
        _validate_original_notify_state(state)
        computer_use = state.get("computer_use")
        if not isinstance(computer_use, dict) or (
            computer_use.get("bundle_id") != COMPUTER_USE_BUNDLE_ID
            or computer_use.get("team_id") != COMPUTER_USE_TEAM_ID
            or not isinstance(computer_use.get("executable"), str)
            or not computer_use.get("executable")
        ):
            raise ValueError("codex-notify 待恢复安装状态中的 Computer Use 身份无效")
        if not self.config_file.exists():
            raise ValueError("存在待恢复安装状态，但 config.toml 不存在")
        try:
            current = tomllib.loads(self.config_file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("无法根据待恢复安装状态检查 config.toml") from exc
        current_notify = current.get("notify")
        if current_notify == list(installed_notify):
            self._write_install_state(state)
            pending_path.unlink()
            return
        if current_notify == list(expected_notify):
            pending_path.unlink()
            return
        raise ValueError(
            "config.toml 与待恢复安装状态均不一致，安装器不会猜测恢复"
        )

    def _read_install_state(self, *, required: bool) -> dict[str, Any]:
        return _load_install_state(self.paths.install_state, required=required)

    def _prepare_config_uninstall(self) -> _ConfigUninstallPlan:
        state = self._read_install_state(required=False)
        if not self.config_file.exists():
            return _ConfigUninstallPlan(None, None)
        current = self.config_file.read_text(encoding="utf-8")
        legacy_removed = _remove_managed_config_block(current)
        if not state:
            if legacy_removed != current:
                return _ConfigUninstallPlan(current, legacy_removed)
            try:
                config = tomllib.loads(current)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"现有 ~/.codex/config.toml 不是有效 TOML：{exc}") from exc
            notify = config.get("notify")
            if self._has_uninstalled_safety_runner():
                direct_notify = _direct_notify_from_safety_chain(
                    notify, runner=self.paths.runner
                )
                if direct_notify is not None:
                    replacement = "notify = " + json.dumps(
                        list(direct_notify), ensure_ascii=False
                    ) + "\n"
                    return _ConfigUninstallPlan(
                        current,
                        _replace_top_level_notify(current, replacement),
                    )
            if _notify_references_runner(notify, self.paths.runner):
                raise ValueError("notify 仍引用 codex-notify runner，卸载器不会留下悬空命令")
            return _ConfigUninstallPlan(current, None)

        try:
            config = tomllib.loads(current)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"现有 ~/.codex/config.toml 不是有效 TOML：{exc}") from exc
        notify = config.get("notify")
        installed = list(_string_tuple(state.get("installed_notify"), "installed_notify"))
        original_tuple, original_source = _validate_original_notify_state(state)
        original = list(original_tuple)
        previous = list(_string_tuple(state.get("previous_notify"), "previous_notify"))
        if notify == installed:
            return _ConfigUninstallPlan(
                current, _replace_top_level_notify(current, original_source)
            )
        if notify == original or notify is None:
            return _ConfigUninstallPlan(current, None)
        if notify == previous:
            return _ConfigUninstallPlan(current, _replace_top_level_notify(current, ""))
        if _notify_references_runner(notify, self.paths.runner):
            raise ValueError("notify 已发生漂移且仍引用 codex-notify runner；卸载器不会猜测修改")
        return _ConfigUninstallPlan(current, None)

    def _apply_config_uninstall(self, plan: _ConfigUninstallPlan) -> None:
        current = (
            self.config_file.read_text(encoding="utf-8")
            if self.config_file.exists()
            else None
        )
        if current != plan.current_document:
            raise RuntimeError("config.toml 在卸载期间发生变化，已停止删除运行入口")
        if plan.updated_document is not None:
            self._write_config(
                plan.updated_document,
                expected_content=plan.current_document,
            )

    def _restore_config_after_failed_uninstall(
        self, plan: _ConfigUninstallPlan
    ) -> list[str]:
        if plan.updated_document is None:
            return []
        try:
            current = self.config_file.read_text(encoding="utf-8")
        except OSError as exc:
            return [f"无法读取 config.toml 以恢复卸载事务：{exc}"]
        if current == plan.current_document:
            return []
        if current != plan.updated_document:
            return ["config.toml 在卸载失败后再次变化，未覆盖外部更新"]
        try:
            self._write_config(
                plan.current_document,
                expected_content=plan.updated_document,
            )
        except Exception as exc:
            return [f"恢复 config.toml 失败：{exc}"]
        return []

    def _assert_config_does_not_reference_runner(self) -> None:
        if not self.config_file.exists():
            return
        try:
            current = self.config_file.read_bytes()
        except OSError as exc:
            raise ValueError(
                "无法在删除 runtime 前确认 config.toml 已解除 runner 引用"
            ) from exc
        if _config_references_runner(current, self.paths.runner):
            raise ValueError(
                "config.toml 在卸载期间再次变化且仍引用 codex-notify runner；"
                "已保留 install-state 和 runtime"
            )

    def _write_config(
        self,
        content: str,
        *,
        expected_content: str | None = None,
    ) -> None:
        if expected_content is not None:
            current = (
                self.config_file.read_text(encoding="utf-8")
                if self.config_file.exists()
                else None
            )
            if current != expected_content:
                raise RuntimeError("config.toml 在写入前发生变化，已停止覆盖")
        if self.config_file.exists():
            self._backup(self.config_file)
        content_bytes = content.encode("utf-8")
        try:
            self._atomic_write_bytes(
                self.config_file,
                content_bytes,
                default_mode=0o600,
                expected_content=(
                    expected_content.encode("utf-8")
                    if expected_content is not None
                    else None
                ),
            )
        except _PublishedConfigError:
            if self._transaction_after_images is not None:
                self._transaction_after_images["notify"] = content_bytes
            raise
        if self._transaction_after_images is not None:
            self._transaction_after_images["notify"] = content_bytes

    def _remove_hooks(self) -> bytes | None:
        document, current_content = self._read_hooks_snapshot()
        self._assert_hooks_unchanged(current_content)
        if current_content is None:
            return None
        hooks = document.get("hooks")
        if not isinstance(hooks, dict):
            return None
        changed = False
        for event_name in OWNED_HOOK_EVENTS:
            current = hooks.get(event_name, [])
            remaining = _without_our_hook(
                current,
                runner=self.paths.runner,
                event_name=event_name,
                validated_items=self._validated_managed_hooks.get(event_name, []),
            )
            if remaining:
                if event_name not in hooks or remaining != current:
                    hooks[event_name] = remaining
                    changed = True
            elif event_name in hooks:
                hooks.pop(event_name, None)
                changed = True
        if changed:
            return self._write_hooks(document, expected_content=current_content)
        return None

    def _restore_hooks_after_failed_uninstall(
        self,
        original_content: bytes | None,
        written_content: bytes | None,
    ) -> list[str]:
        if written_content is None:
            return []
        if original_content is None:
            return ["缺少 hooks.json 的卸载前快照，无法恢复"]
        try:
            current_content = self.hooks_file.read_bytes()
        except OSError as exc:
            return [f"无法读取 hooks.json 以恢复卸载事务：{exc}"]
        if current_content == original_content:
            return []
        if current_content != written_content:
            return ["hooks.json 在卸载失败后再次变化，未覆盖外部更新"]
        try:
            self._atomic_write_bytes(
                self.hooks_file,
                original_content,
                default_mode=0o600,
                expected_content=written_content,
            )
        except Exception as exc:
            return [f"恢复 hooks.json 失败：{exc}"]
        return []

    def _read_hooks(self) -> dict[str, Any]:
        return self._read_hooks_snapshot()[0]

    def _read_hooks_snapshot(self) -> tuple[dict[str, Any], bytes | None]:
        if not self.hooks_file.exists():
            return {}, None
        try:
            content = self.hooks_file.read_bytes()
            document = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"现有 hooks.json 不是有效 JSON：{exc}") from exc
        except OSError as exc:
            raise ValueError(f"无法读取现有 hooks.json：{exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("现有 hooks.json 顶层必须是 object")
        return document, content

    def _assert_hooks_unchanged(self, current_content: bytes | None) -> None:
        if (
            self._validated_hooks_snapshot
            and current_content != self._validated_hooks_content
        ):
            raise RuntimeError("hooks.json 在预检后发生变化，已停止覆盖")

    def _write_hooks(
        self,
        document: dict[str, Any],
        *,
        expected_content: bytes | None = None,
    ) -> bytes:
        self.hooks_file.parent.mkdir(parents=True, exist_ok=True)
        if expected_content is not None:
            self._backup(self.hooks_file)
        content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        self._atomic_write_bytes(
            self.hooks_file,
            content,
            default_mode=0o600,
            expected_content=expected_content,
            expected_absent=expected_content is None,
        )
        if self._transaction_after_images is not None:
            self._transaction_after_images["hooks"] = content
        return content

    def _backup(self, path: Path) -> None:
        backup = path.with_name(f"{path.name}.backup-{time.time_ns()}")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            backup.unlink(missing_ok=True)
            raise
        if self._transaction_backup_files is not None:
            self._transaction_backup_files.append(backup)

    @staticmethod
    def _atomic_write_text(
        path: Path,
        content: str,
        *,
        default_mode: int,
        follow_symlinks: bool = True,
    ) -> None:
        Installer._atomic_write_bytes(
            path,
            content.encode("utf-8"),
            default_mode=default_mode,
            follow_symlinks=follow_symlinks,
        )

    @staticmethod
    def _atomic_write_bytes(
        path: Path,
        content: bytes,
        *,
        default_mode: int,
        expected_content: bytes | None = None,
        expected_absent: bool = False,
        follow_symlinks: bool = True,
    ) -> None:
        if expected_absent and expected_content is not None:
            raise ValueError("expected_content 与 expected_absent 不能同时设置")
        if not follow_symlinks and path.is_symlink():
            raise ValueError(f"安装目标不能是符号链接：{path}")
        destination = (
            path.resolve(strict=False)
            if follow_symlinks and path.is_symlink()
            else path
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        mode = (
            ((destination.stat().st_mode & 0o777) | default_mode) & ~0o022
            if follow_symlinks and destination.exists()
            else default_mode
        )
        temporary = destination.with_name(
            f".{destination.name}.codex-notify-{os.getpid()}-{time.time_ns()}.tmp"
        )
        descriptor = -1
        preserve_temporary = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
            )
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_content is not None:
                _replace_file_if_unchanged(
                    destination,
                    temporary,
                    expected_content=expected_content,
                )
            elif expected_absent:
                try:
                    os.link(temporary, destination)
                except FileExistsError as exc:
                    raise RuntimeError(
                        f"{destination.name} 在写入前发生变化，已停止覆盖"
                    ) from exc
            else:
                temporary.replace(destination)
        except _PreserveTemporaryConfigError:
            preserve_temporary = True
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not preserve_temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # The destination may already contain the published config.
                    # A stale private temporary file is safer than turning cleanup
                    # failure into a transaction failure after publication.
                    pass

    def _launch_agent_payload(
        self,
        executable: Path,
        *,
        label: str = LAUNCH_AGENT_LABEL,
    ) -> dict[str, Any]:
        return {
            "Label": label,
            "ProgramArguments": [
                str(executable),
                str(self.paths.runner),
                "worker",
                "--once",
            ],
            "WorkingDirectory": str(self.paths.root),
            "RunAtLoad": True,
            "StartInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(self.paths.worker_stdout),
            "StandardErrorPath": str(self.paths.worker_stderr),
        }

    def _install_launch_agent(
        self, *, expected_content: bytes | None | object = _UNSET
    ) -> None:
        self.launch_agent.parent.mkdir(parents=True, exist_ok=True)
        payload = self._launch_agent_payload(self.python_executable)
        content = plistlib.dumps(payload, sort_keys=True)
        write_conditions: dict[str, Any] = {}
        if expected_content is None:
            write_conditions["expected_absent"] = True
        elif expected_content is not _UNSET:
            write_conditions["expected_content"] = expected_content
        self._atomic_write_bytes(
            self.launch_agent,
            content,
            default_mode=0o600,
            **write_conditions,
        )
        if self._transaction_after_images is not None:
            self._transaction_after_images["current-launch-agent"] = content
        self.launch_agent.chmod(0o600)

    def _bootout_launch_agent(self) -> bool:
        result = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if self._is_launch_agent_loaded():
            detail = result.stderr.strip() or f"launchctl 退出码 {result.returncode}"
            raise RuntimeError(f"LaunchAgent 停止失败：{detail}")
        return result.returncode == 0

    def _reload_launch_agent(self) -> None:
        self._bootout_launch_agent()
        self._bootstrap_launch_agent()

    def _bootstrap_launch_agent(self) -> None:
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.launch_agent)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LaunchAgent 启动失败：{result.stderr.strip()}")

    def _is_launch_agent_loaded(self) -> bool:
        return self._launch_agent_loaded(LAUNCH_AGENT_LABEL)

    def _launch_agent_loaded(self, label: str) -> bool:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        if _launch_agent_is_missing(result, label=label):
            return False
        detail = result.stderr.strip() or result.stdout.strip() or (
            f"launchctl 退出码 {result.returncode}"
        )
        raise RuntimeError(f"无法确认 LaunchAgent 状态：{detail}")

    def _is_legacy_launch_agent_loaded(self) -> bool:
        return self._launch_agent_loaded(LEGACY_LAUNCH_AGENT_LABEL)

    def _bootout_legacy_launch_agent(self) -> None:
        result = subprocess.run(
            [
                "launchctl",
                "bootout",
                f"gui/{os.getuid()}/{LEGACY_LAUNCH_AGENT_LABEL}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if self._is_legacy_launch_agent_loaded():
            detail = result.stderr.strip() or f"launchctl 退出码 {result.returncode}"
            raise RuntimeError(f"旧 LaunchAgent 停止失败：{detail}")

    def _bootstrap_legacy_launch_agent(self) -> None:
        result = subprocess.run(
            [
                "launchctl",
                "bootstrap",
                f"gui/{os.getuid()}",
                str(self.legacy_launch_agent),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"旧 LaunchAgent 启动失败：{result.stderr.strip()}")

    def _validate_launch_agent(self) -> bytes | None:
        path = self.launch_agent
        if path.is_symlink():
            raise ValueError(f"LaunchAgent 不是普通文件：{path}")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError(f"LaunchAgent 不是普通文件：{path}")
        try:
            content = path.read_bytes()
            payload = plistlib.loads(content)
        except (OSError, plistlib.InvalidFileException) as exc:
            raise ValueError(f"无法确认 LaunchAgent 所有权：{path}") from exc
        arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        if (
            not isinstance(arguments, list)
            or len(arguments) != 4
            or not isinstance(arguments[0], str)
            or not Path(arguments[0]).is_absolute()
            or payload != self._launch_agent_payload(Path(arguments[0]))
        ):
            raise ValueError(f"LaunchAgent 不属于 codex-notify：{path}")
        return content

    def _validate_legacy_launch_agent(self) -> bytes | None:
        path = self.legacy_launch_agent
        if path.is_symlink():
            raise ValueError(f"旧 LaunchAgent 不是普通文件：{path}")
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError(f"旧 LaunchAgent 不是普通文件：{path}")
        try:
            content = path.read_bytes()
            payload = plistlib.loads(content)
        except (OSError, plistlib.InvalidFileException) as exc:
            raise ValueError(f"无法确认旧 LaunchAgent 所有权：{path}") from exc
        arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        if (
            not isinstance(arguments, list)
            or len(arguments) != 4
            or not isinstance(arguments[0], str)
            or not Path(arguments[0]).is_absolute()
            or payload
            != self._launch_agent_payload(
                Path(arguments[0]),
                label=LEGACY_LAUNCH_AGENT_LABEL,
            )
        ):
            raise ValueError(f"旧 LaunchAgent 不属于 codex-notify：{path}")
        return content

    def _validate_source(self) -> None:
        if not self.package_dir.is_dir() or not (
            self.package_dir / "__init__.py"
        ).is_file():
            raise FileNotFoundError(f"找不到 codex_notify 包目录：{self.package_dir}")

    def _validate_hooks_document(self, document: dict[str, Any]) -> None:
        hooks = document.get("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("现有 hooks.json 的 hooks 字段必须是 object")
        validated_managed_hooks: dict[str, list[dict[str, Any]]] = {}
        for event_name in OWNED_HOOK_EVENTS:
            if event_name in hooks and not isinstance(hooks[event_name], list):
                raise ValueError(f"现有 hooks.json 的 {event_name} 字段必须是 array")
        for event_name, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                if not isinstance(handlers, list):
                    continue
                for handler in handlers:
                    if _is_our_hook(
                        handler,
                        runner=self.paths.runner,
                        event_name=event_name,
                    ):
                        if event_name not in set(OWNED_HOOK_EVENTS):
                            raise ValueError(
                                f"现有 hooks.json 的 {event_name} codex-notify Hook 已漂移"
                            )
                        expected_current = is_expected_managed_hook(
                            handler, runner=self.paths.runner, event_name=event_name
                        )
                        expected_legacy_stop = (
                            event_name == "Stop"
                            and _is_expected_legacy_stop_hook(
                                handler, runner=self.paths.runner
                            )
                        )
                        if not expected_current and not expected_legacy_stop:
                            raise ValueError(
                                f"现有 hooks.json 的 {event_name} codex-notify Hook 已漂移"
                            )
                        if event_name == "PreToolUse" and group.get("matcher") != "Bash":
                            raise ValueError(
                                "现有 hooks.json 的 PreToolUse codex-notify Hook 已漂移"
                            )
                        if (
                            event_name == "PermissionRequest"
                            and group.get("matcher") != ".*"
                        ):
                            raise ValueError(
                                "现有 hooks.json 的 PermissionRequest codex-notify Hook 已漂移"
                            )
                        validated_managed_hooks.setdefault(event_name, []).append(
                            dict(handler)
                        )
                    elif _is_runner_hook_reference(
                        handler, runner=self.paths.runner
                    ):
                        raise ValueError(
                            f"现有 hooks.json 的 {event_name} codex-notify Hook 已漂移"
                        )
        self._validated_managed_hooks = validated_managed_hooks

    @staticmethod
    def _write_destination(path: Path) -> Path:
        return path.resolve(strict=False) if path.is_symlink() else path

    def _snapshot_install_targets(
        self,
        *,
        include_legacy_cli: bool = False,
        include_legacy_launch_agent: bool = False,
        include_runtime_data: bool = False,
    ) -> tuple[Path, list[tuple[Path, str, Path | None]]]:
        backup_root = Path(tempfile.mkdtemp(prefix="codex-notify-install-backup-"))
        snapshots: list[tuple[Path, str, Path | None]] = []
        targets = [
            self.paths.library_dir,
            self.paths.runner,
            self.paths.install_state,
            self.paths.pending_install_state,
            self.config_file,
            self.hooks_file,
            self.launch_agent,
        ]
        if include_legacy_launch_agent:
            targets.append(self.legacy_launch_agent)
        if include_legacy_cli:
            targets.append(self.cli_path)
        if include_runtime_data:
            targets.extend((self.paths.data_dir, self.paths.log_dir))
        try:
            for index, logical_path in enumerate(targets):
                destination = self._write_destination(logical_path)
                backup = backup_root / str(index)
                if destination.is_dir():
                    shutil.copytree(destination, backup)
                    snapshots.append((destination, "directory", backup))
                elif destination.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup)
                    snapshots.append((destination, "file", backup))
                else:
                    snapshots.append((destination, "missing", None))
            return backup_root, snapshots
        except Exception:
            shutil.rmtree(backup_root, ignore_errors=True)
            raise

    def _snapshot_runtime_directories(self) -> list[tuple[Path, str, int | None]]:
        snapshots: list[tuple[Path, str, int | None]] = []
        for path in (self.paths.root, self.paths.data_dir, self.paths.log_dir):
            if path.is_dir():
                snapshots.append((path, "directory", path.stat().st_mode & 0o777))
            elif path.exists() or path.is_symlink():
                snapshots.append((path, "other", None))
            else:
                snapshots.append((path, "missing", None))
        return snapshots

    def _restore_uninstall_snapshot(
        self,
        logical_path: Path,
        snapshots: list[tuple[Path, str, Path | None]],
        *,
        expected_after: bytes | None = None,
        expected_absent: bool = False,
    ) -> list[str]:
        destination = self._write_destination(logical_path)
        snapshot = next(
            (
                item
                for item in snapshots
                if item[0] == destination
            ),
            None,
        )
        if snapshot is None:
            return [f"缺少 {logical_path} 的卸载前所有权快照"]
        _, kind, backup = snapshot
        try:
            if kind == "file" and backup is not None:
                before = backup.read_bytes()
                if destination.is_file() and not destination.is_symlink():
                    current = destination.read_bytes()
                    if current == before:
                        return []
                    if expected_absent or current != expected_after:
                        return [f"{logical_path} 在卸载失败后被并发替换，未覆盖"]
                    self._atomic_write_bytes(
                        destination,
                        before,
                        default_mode=backup.stat().st_mode & 0o777,
                        expected_content=expected_after,
                    )
                    return []
                if destination.exists() or destination.is_symlink():
                    return [f"{logical_path} 在卸载失败后被并发替换，未覆盖"]
                if not expected_absent:
                    return [f"{logical_path} 在卸载失败后意外消失，未猜测恢复"]
                self._atomic_write_bytes(
                    destination,
                    before,
                    default_mode=backup.stat().st_mode & 0o777,
                    expected_absent=True,
                )
                return []
            if kind == "directory" and backup is not None:
                if (
                    destination.is_dir()
                    and not destination.is_symlink()
                    and _directory_fingerprint(destination)
                    == _directory_fingerprint(backup)
                ):
                    return []
                if destination.exists() or destination.is_symlink():
                    return [f"{logical_path} 在卸载失败后被并发替换，未覆盖"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(backup, destination)
                return []
            if kind == "missing":
                if not (destination.exists() or destination.is_symlink()):
                    return []
                if expected_after is None:
                    return [f"{logical_path} 在卸载失败后被并发创建，未删除"]
                if destination.is_symlink() or not destination.is_file():
                    return [f"{logical_path} 在卸载失败后被并发替换，未删除"]
                if destination.read_bytes() != expected_after:
                    return [f"{logical_path} 在卸载失败后被并发替换，未删除"]
                _remove_file_if_unchanged(
                    destination,
                    expected_content=expected_after,
                )
                return []
            return [f"不支持恢复 {logical_path} 的 {kind} 快照"]
        except Exception as exc:
            return [f"恢复 {logical_path} 失败：{exc}"]

    def _rollback_uninstall_transaction(
        self,
        *,
        snapshots: list[tuple[Path, str, Path | None]],
        runtime_directories: list[tuple[Path, str, int | None]],
        config_action: _ConfigUninstallPlan,
        hooks_content: bytes | None,
        removed_hooks_content: bytes | None,
        launch_agent_content: bytes | None,
        legacy_cli_content: bytes | None,
        legacy_launch_agent_content: bytes | None,
        agent_stopped: bool,
        legacy_agent_stopped: bool,
        retain_safety_runner: bool,
        remove_runner: bool,
        purge: bool,
    ) -> list[str]:
        journal = _RollbackJournal(
            operation="uninstall",
            checkpoint=self._transaction_checkpoint,
        )

        def restore_current_service() -> list[str]:
            if not agent_stopped:
                return []
            try:
                if launch_agent_content is None:
                    raise RuntimeError("缺少 LaunchAgent 的预检快照")
                self._assert_migration_file_unchanged(
                    self.launch_agent,
                    launch_agent_content,
                    "LaunchAgent",
                )
                self._bootstrap_launch_agent()
                return []
            except Exception as exc:
                return [f"LaunchAgent 恢复失败：{exc}"]

        def restore_legacy_service() -> list[str]:
            if not legacy_agent_stopped:
                return []
            try:
                if legacy_launch_agent_content is None:
                    raise RuntimeError("缺少旧 LaunchAgent 的预检快照")
                self._assert_migration_file_unchanged(
                    self.legacy_launch_agent,
                    legacy_launch_agent_content,
                    "旧 LaunchAgent",
                )
                self._bootstrap_legacy_launch_agent()
                return []
            except Exception as exc:
                return [f"旧 LaunchAgent 恢复失败：{exc}"]

        if legacy_agent_stopped:
            journal.record(
                step="stop-legacy-service",
                resource="legacy-service",
                transition=("loaded", "unloaded"),
                compensate=restore_legacy_service,
            )
        if agent_stopped:
            journal.record(
                step="stop-current-service",
                resource="current-service",
                transition=("loaded", "unloaded"),
                compensate=restore_current_service,
            )
        journal.record(
            step="restore-notify",
            resource="notify",
            transition=(
                self._ownership_snapshots["notify"].state,
                "computer-use"
                if config_action.updated_document is not None
                else self._ownership_snapshots["notify"].state,
            ),
            compensate=lambda: self._restore_config_after_failed_uninstall(
                config_action
            ),
        )
        journal.record(
            step="remove-hooks",
            resource="hooks",
            transition=(
                "managed" if hooks_content is not None else "absent",
                "shared" if hooks_content is not None else "managed",
            ),
            compensate=lambda: self._restore_hooks_after_failed_uninstall(
                hooks_content,
                removed_hooks_content,
            ),
        )
        if launch_agent_content is not None:
            journal.record(
                step="remove-current-launch-agent",
                resource="current-launch-agent",
                transition=("owned", "absent"),
                compensate=lambda: self._restore_uninstall_snapshot(
                    self.launch_agent,
                    snapshots,
                    expected_absent=True,
                ),
            )
        if legacy_launch_agent_content is not None:
            journal.record(
                step="remove-legacy-launch-agent",
                resource="legacy-launch-agent",
                transition=("owned", "absent"),
                compensate=lambda: self._restore_uninstall_snapshot(
                    self.legacy_launch_agent,
                    snapshots,
                    expected_absent=True,
                ),
            )
        if legacy_cli_content is not None:
            journal.record(
                step="remove-legacy-cli",
                resource="cli",
                transition=("owned", "absent"),
                compensate=lambda: self._restore_uninstall_snapshot(
                    self.cli_path,
                    snapshots,
                    expected_absent=True,
                ),
            )
        runner_after = (
            UNINSTALLED_RUNNER_CONTENT.encode("utf-8")
            if retain_safety_runner
            else None
        )
        if retain_safety_runner or remove_runner:
            journal.record(
                step=(
                    "publish-safety-runner"
                    if retain_safety_runner
                    else "remove-runner"
                ),
                resource="runtime",
                transition=("owned", "owned" if retain_safety_runner else "absent"),
                compensate=lambda: self._restore_uninstall_snapshot(
                    self.paths.runner,
                    snapshots,
                    expected_after=runner_after,
                    expected_absent=remove_runner,
                ),
                barrier=True,
            )
        for path in (self.paths.install_state, self.paths.pending_install_state):
            journal.record(
                step="remove-install-state",
                resource="runtime",
                transition=("owned", "absent"),
                compensate=lambda path=path: self._restore_uninstall_snapshot(
                    path,
                    snapshots,
                    expected_absent=True,
                ),
                barrier=True,
            )
        journal.record(
            step="remove-runtime",
            resource="runtime",
            transition=(
                self._ownership_snapshots["runtime"].state,
                "absent",
            ),
            compensate=lambda: self._restore_uninstall_snapshot(
                self.paths.library_dir,
                snapshots,
                expected_absent=True,
            ),
            barrier=True,
        )
        if purge:
            for step, path in (
                ("purge-data", self.paths.data_dir),
                ("purge-logs", self.paths.log_dir),
            ):
                journal.record(
                    step=step,
                    resource="runtime",
                    transition=("owned", "absent"),
                    compensate=lambda path=path: self._restore_uninstall_snapshot(
                        path,
                        snapshots,
                        expected_absent=True,
                    ),
                    barrier=True,
                )
        errors = journal.rollback()
        if not errors:
            errors.extend(self._restore_runtime_directories(runtime_directories))
        return errors

    def _restore_runtime_directories(
        self,
        snapshots: list[tuple[Path, str, int | None]],
    ) -> list[str]:
        errors: list[str] = []
        for path, kind, mode in reversed(snapshots):
            try:
                if kind == "directory":
                    if not path.is_dir() or mode is None:
                        raise RuntimeError("原有目录已不存在")
                    path.chmod(mode)
                elif kind == "missing" and (path.exists() or path.is_symlink()):
                    if not path.is_dir() or path.is_symlink():
                        raise RuntimeError("安装期间出现了非目录目标")
                    if (
                        path == self.paths.root
                        and self._has_uninstalled_safety_runner()
                    ):
                        continue
                    path.rmdir()
            except Exception as exc:
                errors.append(f"恢复运行目录 {path} 失败：{exc}")
        return errors

    @staticmethod
    def _remove_install_target(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def _rollback_install(
        self,
        backup_root: Path,
        snapshots: list[tuple[Path, str, Path | None]],
    ) -> list[str]:
        errors: list[str] = []
        config_destination = self._write_destination(self.config_file)
        hooks_destination = self._write_destination(self.hooks_file)
        launch_agent_destination = self._write_destination(self.launch_agent)
        after_images = self._transaction_after_images or {}
        written_config = after_images.get("notify")
        runner_destination = self._write_destination(self.paths.runner)
        migration_destinations = {
            self._write_destination(self.cli_path),
            self._write_destination(self.legacy_launch_agent),
        }
        runner_was_missing = any(
            destination == runner_destination and kind == "missing"
            for destination, kind, _ in snapshots
        )
        config_snapshot = next(
            (
                backup
                for destination, kind, backup in snapshots
                if destination == config_destination and kind == "file"
            ),
            None,
        )
        try:
            current_config = config_destination.read_bytes()
        except OSError as exc:
            return [
                "无法读取安装后 config.toml，不能确认通知链已解除；"
                "已保留 runtime 和状态",
                f"读取失败：{exc}",
                f"回滚备份保留在 {backup_root}",
            ]
        expected_config = written_config
        if expected_config is None and config_snapshot is not None:
            try:
                expected_config = config_snapshot.read_bytes()
            except OSError:
                expected_config = None
        if (
            current_config != expected_config
            and _config_references_runner(current_config, self.paths.runner)
        ):
            return [
                "config.toml 已被并发安装或外部进程修改且仍引用 "
                "codex-notify runner；为避免悬空通知链，已保留 runtime 和状态",
                f"回滚备份保留在 {backup_root}",
            ]
        leave_safety_runner = written_config is not None and runner_was_missing
        if leave_safety_runner:
            try:
                self._install_uninstalled_safety_runner()
            except Exception as exc:
                return [
                    "无法在回滚前安装安全 runner；已保留 runtime 和状态",
                    f"安装安全 runner 失败：{exc}",
                    f"回滚备份保留在 {backup_root}",
                ]
        for destination, kind, backup in reversed(snapshots):
            try:
                if destination == config_destination:
                    written = after_images.get("notify")
                    if written is None:
                        continue
                    try:
                        if not destination.is_file():
                            continue
                        current = destination.read_bytes()
                    except OSError as exc:
                        return [
                            "回滚期间无法读取 config.toml，不能安全删除 runtime；"
                            "已保留 runtime 和状态",
                            f"读取失败：{exc}",
                            f"回滚备份保留在 {backup_root}",
                        ]
                    if current != written:
                        if _config_references_runner(current, self.paths.runner):
                            return [
                                "config.toml 在回滚期间被外部修改且仍引用 "
                                "codex-notify runner；已保留 runtime 和状态",
                                f"回滚备份保留在 {backup_root}",
                            ]
                        continue
                    if kind == "file" and backup is not None:
                        try:
                            self._atomic_write_bytes(
                                destination,
                                backup.read_bytes(),
                                default_mode=0o600,
                                expected_content=written,
                            )
                        except Exception as exc:
                            try:
                                current = destination.read_bytes()
                            except OSError as read_exc:
                                return [
                                    "原子回滚失败后无法读取 config.toml；"
                                    "已保留 runtime 和状态",
                                    f"回滚失败：{exc}",
                                    f"读取失败：{read_exc}",
                                    f"回滚备份保留在 {backup_root}",
                                ]
                            if _config_references_runner(
                                current, self.paths.runner
                            ):
                                return [
                                    "config.toml 在原子回滚期间仍引用 "
                                    "codex-notify runner；已保留 runtime 和状态",
                                    f"回滚备份保留在 {backup_root}",
                                ]
                            errors.append(f"恢复 {destination} 失败：{exc}")
                        continue
                if destination == hooks_destination:
                    written_hooks = after_images.get("hooks")
                    if written_hooks is None:
                        continue
                    try:
                        if not destination.is_file():
                            return [
                                "安装后 hooks.json 已消失，不能安全回滚；"
                                "已保留 runtime 和状态",
                                f"回滚备份保留在 {backup_root}",
                            ]
                        current_hooks = destination.read_bytes()
                    except OSError as exc:
                        return [
                            "回滚期间无法读取 hooks.json；已保留 runtime 和状态",
                            f"读取失败：{exc}",
                            f"回滚备份保留在 {backup_root}",
                        ]
                    if current_hooks != written_hooks:
                        return [
                            "hooks.json 在安装后被外部修改；已保留 runtime 和状态",
                            f"回滚备份保留在 {backup_root}",
                        ]
                    if kind == "file" and backup is not None:
                        self._atomic_write_bytes(
                            destination,
                            backup.read_bytes(),
                            default_mode=0o600,
                            expected_content=written_hooks,
                        )
                    elif kind == "missing":
                        _remove_file_if_unchanged(
                            destination,
                            expected_content=written_hooks,
                        )
                    continue
                if destination == launch_agent_destination:
                    written_launch_agent = after_images.get(
                        "current-launch-agent"
                    )
                    if written_launch_agent is None:
                        continue
                    if destination.is_symlink() or not destination.is_file():
                        errors.append(
                            "回滚期间 LaunchAgent 已被并发替换，未覆盖外部文件"
                        )
                        continue
                    try:
                        current_launch_agent = destination.read_bytes()
                    except OSError as exc:
                        errors.append(f"无法读取 LaunchAgent 以回滚：{exc}")
                        continue
                    if current_launch_agent != written_launch_agent:
                        errors.append(
                            "回滚期间 LaunchAgent 已被并发替换，未覆盖外部文件"
                        )
                        continue
                    if kind == "file" and backup is not None:
                        self._atomic_write_bytes(
                            destination,
                            backup.read_bytes(),
                            default_mode=0o600,
                            expected_content=written_launch_agent,
                        )
                    elif kind == "missing":
                        _remove_file_if_unchanged(
                            destination,
                            expected_content=written_launch_agent,
                        )
                    continue
                if destination == runner_destination and leave_safety_runner:
                    continue
                if (
                    destination in migration_destinations
                    and kind == "file"
                    and backup is not None
                ):
                    if destination.exists() or destination.is_symlink():
                        try:
                            unchanged = (
                                not destination.is_symlink()
                                and destination.read_bytes() == backup.read_bytes()
                            )
                        except OSError:
                            unchanged = False
                        if not unchanged:
                            errors.append(
                                f"回滚期间 {destination} 已被并发替换，未覆盖外部文件"
                            )
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    backup_content = backup.read_bytes()
                    backup_mode = backup.stat().st_mode & 0o777
                    self._atomic_write_bytes(
                        destination,
                        backup_content,
                        default_mode=backup_mode,
                        expected_absent=True,
                    )
                    continue
                self._remove_install_target(destination)
                if kind == "directory" and backup is not None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(backup, destination)
                elif kind == "file" and backup is not None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, destination)
            except Exception as exc:
                errors.append(f"恢复 {destination} 失败：{exc}")
        if errors:
            errors.append(f"回滚备份保留在 {backup_root}")
        else:
            shutil.rmtree(backup_root, ignore_errors=True)
        return errors


def _without_our_hook(
    groups: Any,
    *,
    runner: Path,
    event_name: str,
    validated_items: list[dict[str, Any]] | None = None,
) -> list[Any]:
    if not isinstance(groups, list):
        raise ValueError(f"现有 hooks.json 的 {event_name} 字段必须是 array")
    output: list[Any] = []
    for group in groups:
        if not isinstance(group, dict):
            output.append(group)
            continue
        commands = group.get("hooks")
        if not isinstance(commands, list):
            output.append(group)
            continue
        remaining = [
            item
            for item in commands
            if not (
                item in (validated_items or [])
                or _is_our_hook(item, runner=runner, event_name=event_name)
            )
        ]
        if len(remaining) == len(commands):
            output.append(group)
        elif remaining:
            copy = dict(group)
            copy["hooks"] = remaining
            output.append(copy)
    return output


def _is_our_hook(item: Any, *, runner: Path, event_name: str) -> bool:
    if not isinstance(item, dict) or item.get("type") != "command":
        return False
    command = item.get("command")
    if not isinstance(command, str):
        return False
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    expected_interpreter = _runner_shebang_interpreter(runner)
    return (
        len(arguments) == 4
        and expected_interpreter is not None
        and arguments[0] == expected_interpreter
        and arguments[1] == str(runner)
        and arguments[2:] == ["hook", event_name]
    )


def _is_runner_hook_reference(item: Any, *, runner: Path) -> bool:
    if not isinstance(item, dict):
        return False
    command = item.get("command")
    if not isinstance(command, str):
        return False
    try:
        arguments = shlex.split(command)
    except ValueError:
        return str(runner) in command and "hook" in command
    if len(arguments) >= 2 and arguments[:2] == [str(runner), "hook"]:
        return True
    script_index = _python_command_script_index(arguments)
    return bool(
        script_index is not None
        and script_index + 1 < len(arguments)
        and arguments[script_index] == str(runner)
        and arguments[script_index + 1] == "hook"
    )


def _runner_shebang_interpreter(runner: Path) -> str | None:
    try:
        with runner.open("r", encoding="utf-8") as handle:
            first_line = handle.readline(4096)
    except (OSError, UnicodeError):
        return None
    if not first_line.endswith("\n") or not first_line.startswith("#!/"):
        return None
    interpreter = first_line[2:].strip()
    return interpreter if interpreter and Path(interpreter).is_absolute() else None


def _python_command_script_index(arguments: list[str]) -> int | None:
    script_index = _python_script_index(arguments)
    if script_index is not None:
        return script_index
    command_index = _env_command_index(arguments)
    if command_index is None:
        return None
    nested_index = _python_script_index(arguments[command_index:])
    return command_index + nested_index if nested_index is not None else None


def _env_command_index(arguments: list[str]) -> int | None:
    if not arguments or Path(arguments[0]).name.lower() != "env":
        return None
    index = 1
    options_without_value = {
        "-0",
        "--debug",
        "-i",
        "--ignore-environment",
        "--null",
        "-v",
    }
    options_with_value = {"-C", "--chdir", "-u", "--unset"}
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return index + 1 if index + 1 < len(arguments) else None
        if argument in options_without_value:
            index += 1
            continue
        if argument in options_with_value:
            index += 2
            continue
        if argument.startswith(("--chdir=", "--unset=")):
            index += 1
            continue
        if argument.startswith("-"):
            return None
        name, separator, _ = argument.partition("=")
        if separator and _is_environment_name(name):
            index += 1
            continue
        return index
    return None


def _is_environment_name(name: str) -> bool:
    return bool(
        name
        and (name[0].isalpha() or name[0] == "_")
        and all(character.isalnum() or character == "_" for character in name[1:])
    )


def _python_script_index(arguments: list[str]) -> int | None:
    if not arguments:
        return None
    executable = Path(arguments[0]).name.lower()
    if executable != "python" and not executable.startswith("python3"):
        return None
    index = 1
    options_with_value = {"-W", "-X", "--check-hash-based-pycs"}
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return index + 1 if index + 1 < len(arguments) else None
        if not argument.startswith("-") or argument == "-":
            return index
        if argument in {"-c", "-m"}:
            return None
        if argument in options_with_value:
            index += 2
        else:
            index += 1
    return None


def is_expected_managed_hook(
    item: Any, *, runner: Path, event_name: str
) -> bool:
    if not _is_our_hook(item, runner=runner, event_name=event_name):
        return False
    expected = {
        "type": "command",
        "command": item["command"],
        "timeout": 5,
    }
    if event_name == "UserPromptSubmit":
        expected["statusMessage"] = HOOK_STATUS_START
    elif event_name == "PermissionRequest":
        expected["statusMessage"] = HOOK_STATUS_PERMISSION
    return item == expected


def _is_expected_legacy_stop_hook(item: Any, *, runner: Path) -> bool:
    if not _is_our_hook(item, runner=runner, event_name="Stop"):
        return False
    return item == {
        "type": "command",
        "command": item["command"],
        "timeout": 5,
        "statusMessage": LEGACY_STOP_STATUS_MESSAGE,
    }


def _is_managed_cli_content(content: str, *, runner: Path) -> bool:
    lines = content.splitlines()
    if (
        len(lines) == 3
        and lines[0].startswith("#!/")
        and Path(lines[0][2:]).is_absolute()
        and lines[1] == "import runpy"
        and lines[2]
        == f"runpy.run_path({str(runner)!r}, run_name=\"__main__\")"
    ):
        return True
    if len(lines) == 2 and lines[0] == "#!/bin/sh":
        command = lines[1]
    elif (
        len(lines) == 3
        and lines[0] == "#!/bin/sh"
        and lines[1] == CLI_MARKER
    ):
        command = lines[2]
    else:
        return False
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    return (
        len(arguments) == 4
        and arguments[0] == "exec"
        and Path(arguments[1]).is_absolute()
        and arguments[2] == str(runner)
        and arguments[3] == "$@"
    )


def _is_managed_runner_content(
    content: str,
    *,
    root: Path,
    library_dir: Path,
) -> bool:
    lines = content.splitlines()
    return (
        len(lines) == 7
        and lines[0].startswith("#!/")
        and Path(lines[0][2:]).is_absolute()
        and lines[1:] == [
            "import os",
            "import sys",
            f'os.environ["CODEX_NOTIFY_HOME"] = {str(root)!r}',
            f"sys.path.insert(0, {str(library_dir)!r})",
            "from codex_notify.cli import main",
            "raise SystemExit(main())",
        ]
    )


def _hooks_explicitly_disabled(config: dict[str, Any]) -> bool:
    features = config.get("features")
    if not isinstance(features, dict):
        return False
    if features.get("hooks") is False:
        return True
    return "hooks" not in features and features.get("codex_hooks") is False


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"codex-notify 安装状态中的 {field} 无效")
    return tuple(value)


def _load_install_state(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ValueError("缺少 codex-notify 安装状态，无法安全修改通知链")
        return {}
    if path.is_symlink():
        raise ValueError("codex-notify 安装状态不能是符号链接")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("codex-notify 安装状态无效") from exc
    if not isinstance(state, dict) or state.get("schema_version") not in {
        INSTALL_STATE_VERSION,
        LEGACY_INSTALL_STATE_VERSION,
    }:
        raise ValueError("codex-notify 安装状态版本不受支持")
    if state.get("schema_version") == INSTALL_STATE_VERSION and (
        not isinstance(state.get("runtime_version"), str)
        or not state["runtime_version"]
    ):
        raise ValueError("codex-notify 安装状态缺少 runtime_version")
    return state


def _validate_install_state_for_integration(
    state: dict[str, Any],
    integration: ComputerUseIntegration,
) -> tuple[tuple[str, ...], str]:
    installed_notify = _string_tuple(state.get("installed_notify"), "installed_notify")
    previous_notify = _string_tuple(state.get("previous_notify"), "previous_notify")
    if installed_notify != integration.notify:
        raise ValueError("Computer Use 通知链与 codex-notify 安装状态不一致")
    if previous_notify != integration.previous_notify:
        raise ValueError("codex-notify previous notifier 与安装状态不一致")
    computer_use = state.get("computer_use")
    if not isinstance(computer_use, dict) or (
        computer_use.get("executable") != str(integration.executable)
        or computer_use.get("bundle_id") != COMPUTER_USE_BUNDLE_ID
        or computer_use.get("team_id") != COMPUTER_USE_TEAM_ID
    ):
        raise ValueError("Computer Use 身份与 codex-notify 安装状态不一致")
    original_notify, original_source = _validate_original_notify_state(state)
    return original_notify, original_source


def _validate_original_notify_state(
    state: dict[str, Any],
) -> tuple[tuple[str, ...], str]:
    original_notify = _string_tuple(state.get("original_notify"), "original_notify")
    original_source = state.get("original_notify_source")
    if not isinstance(original_source, str) or not original_source:
        raise ValueError("codex-notify 安装状态缺少原始 notify 配置")
    try:
        parsed = tomllib.loads(original_source)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("codex-notify 原始 notify 配置不是有效 TOML") from exc
    parsed_notify = parsed.get("notify")
    if set(parsed) != {"notify"} or not isinstance(parsed_notify, list) or (
        tuple(parsed_notify) != original_notify
    ):
        raise ValueError("codex-notify 原始 notify 配置与安装状态不一致")
    return original_notify, original_source


def _notify_references_runner(notify: Any, runner: Path) -> bool:
    resolved_runner = runner.resolve(strict=False)
    if _notify_value_references_runner(notify, runner, resolved_runner):
        return True
    if not isinstance(notify, (list, tuple)) or not all(
        isinstance(argument, str) for argument in notify
    ):
        return False
    for index, argument in enumerate(notify[:-1]):
        if argument != PREVIOUS_NOTIFY_FLAG:
            continue
        try:
            previous_notify = decode_previous_notify(notify[index + 1])
        except ValueError:
            continue
        if any(
            _argument_references_runner(item, runner, resolved_runner)
            for item in previous_notify
        ):
            return True
    return False


def _notify_value_references_runner(
    value: Any,
    runner: Path,
    resolved_runner: Path,
    *,
    depth: int = 0,
) -> bool:
    if depth > 16:
        return False
    if isinstance(value, str):
        return _argument_references_runner(value, runner, resolved_runner)
    if isinstance(value, (list, tuple)):
        return any(
            _notify_value_references_runner(
                item,
                runner,
                resolved_runner,
                depth=depth + 1,
            )
            for item in value
        )
    if isinstance(value, dict):
        return any(
            _notify_value_references_runner(
                item,
                runner,
                resolved_runner,
                depth=depth + 1,
            )
            for item in value.values()
        )
    return False


def _argument_references_runner(
    argument: str,
    runner: Path,
    resolved_runner: Path,
) -> bool:
    if str(runner) in argument:
        return True
    candidate = Path(argument)
    if not candidate.is_absolute():
        return False
    try:
        return candidate.resolve(strict=False) == resolved_runner
    except (OSError, RuntimeError, ValueError):
        return False


def _config_references_runner(document: bytes, runner: Path) -> bool:
    try:
        config = tomllib.loads(document.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return True
    return _notify_references_runner(config.get("notify"), runner)


def _is_legacy_managed_notify(notify: Any, *, runner: Path) -> bool:
    return (
        isinstance(notify, list)
        and len(notify) == 3
        and all(isinstance(argument, str) for argument in notify)
        and Path(notify[0]).is_absolute()
        and notify[1:] == [str(runner), "notify"]
    )


def _direct_notify_from_safety_chain(
    notify: Any,
    *,
    runner: Path,
) -> tuple[str, str] | None:
    if not (
        isinstance(notify, list)
        and len(notify) == 4
        and all(isinstance(argument, str) for argument in notify)
        and Path(notify[0]).is_absolute()
        and notify[1] == "turn-ended"
        and notify[2] == PREVIOUS_NOTIFY_FLAG
    ):
        return None
    try:
        previous_notify = decode_previous_notify(notify[3])
    except ValueError:
        return None
    if not _is_legacy_managed_notify(list(previous_notify), runner=runner):
        return None
    return notify[0], "turn-ended"


def _top_level_notify_assignment(document: str) -> tuple[int, int, str]:
    lines = document.splitlines(keepends=True)
    outside_string = _toml_lines_outside_multiline_strings(lines)
    array_depth = _toml_array_depths(lines)
    offset = 0
    start: int | None = None
    start_line: int | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if (
            outside_string[index]
            and array_depth[index] == 0
            and stripped.startswith("[")
        ):
            break
        if outside_string[index] and _is_notify_assignment_start(line):
            start = offset
            start_line = index
            break
        offset += len(line)
    if start is None or start_line is None:
        raise ValueError("无法定位顶层 notify 赋值")
    candidate = ""
    for end_line in range(start_line, len(lines)):
        candidate += lines[end_line]
        try:
            parsed = tomllib.loads(candidate)
        except tomllib.TOMLDecodeError:
            continue
        if set(parsed) == {"notify"}:
            end = start + len(candidate)
            return start, end, document[start:end]
        break
    raise ValueError("无法安全解析顶层 notify 赋值")


def _is_notify_assignment_start(line: str) -> bool:
    stripped = line.lstrip()
    for key in ("notify", '"notify"', "'notify'"):
        if stripped.startswith(key):
            remainder = stripped[len(key) :].lstrip()
            return remainder.startswith("=")
    return False


def _replace_top_level_notify(document: str, replacement: str) -> str:
    start, end, _ = _top_level_notify_assignment(document)
    suffix = document[end:]
    if (
        replacement
        and suffix
        and not replacement.endswith(("\n", "\r"))
        and not suffix.startswith(("\n", "\r"))
    ):
        replacement += "\r\n" if "\r\n" in document else "\n"
    return document[:start] + replacement + suffix


def _launch_agent_is_missing(
    result: subprocess.CompletedProcess[str], *, label: str = LAUNCH_AGENT_LABEL
) -> bool:
    return (
        result.returncode == 113
        and "could not find service" in result.stderr.lower()
        and label in result.stderr
    )


def _swap_directories(staging: Path, destination: Path) -> None:
    """Atomically exchange staged and active runtimes on macOS."""
    if _exchange_paths(staging, destination):
        return

    previous = destination.with_name(
        f".{destination.name}-previous-{os.getpid()}-{time.time_ns()}"
    )
    destination.replace(previous)
    try:
        staging.replace(destination)
    except Exception:
        previous.replace(destination)
        raise
    else:
        shutil.rmtree(previous)


def _directory_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        if candidate.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(candidate).encode("utf-8"))
        elif candidate.is_dir():
            digest.update(b"directory\0")
        elif candidate.is_file():
            digest.update(b"file\0")
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"other\0")
    return digest.hexdigest()


def _replace_file_if_unchanged(
    destination: Path,
    temporary: Path,
    *,
    expected_content: bytes,
) -> None:
    display_name = destination.name
    try:
        actual_content = destination.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"{display_name} 在写入前发生变化，已停止覆盖") from exc
    if actual_content != expected_content:
        raise RuntimeError(f"{display_name} 在写入前发生变化，已停止覆盖")

    try:
        published_fingerprint = _path_fingerprint(temporary)
    except OSError as exc:
        raise RuntimeError(f"无法验证待安装的 {display_name}") from exc
    if _exchange_paths(temporary, destination):
        try:
            displaced_content = temporary.read_bytes()
        except OSError:
            displaced_content = None
        if displaced_content != expected_content:
            _restore_latest_displaced_file(
                temporary,
                destination,
                expected_destination=published_fingerprint,
            )
            raise RuntimeError(
                f"{display_name} 在替换期间发生并发写入，已停止覆盖"
            )
        return

    # Computer Use is macOS-only, but keep a portable fallback for unit tests
    # and source inspection on other platforms. os.replace keeps the canonical
    # path continuously present; the second comparison narrows the non-macOS
    # compare/replace race as far as the portable APIs allow.
    try:
        actual_content = destination.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"{display_name} 在写入前发生变化，已停止覆盖") from exc
    if actual_content != expected_content:
        raise RuntimeError(f"{display_name} 在写入前发生变化，已停止覆盖")
    temporary.replace(destination)


def _remove_file_if_unchanged(
    destination: Path,
    *,
    expected_content: bytes,
) -> None:
    display_name = destination.name
    displaced = destination.with_name(
        f".{destination.name}.codex-notify-rollback-"
        f"{os.getpid()}-{time.time_ns()}.tmp"
    )
    try:
        destination.replace(displaced)
    except OSError as exc:
        raise RuntimeError(
            f"{display_name} 在删除前发生变化，已停止删除"
        ) from exc

    preserve_displaced = False
    try:
        try:
            displaced_content = displaced.read_bytes()
        except OSError as exc:
            preserve_displaced = True
            raise _PreserveTemporaryConfigError(
                f"无法验证待删除的 {display_name}；文件保留在 {displaced}"
            ) from exc
        if not displaced.is_symlink() and displaced_content == expected_content:
            return

        try:
            os.link(displaced, destination, follow_symlinks=False)
        except FileExistsError as exc:
            preserve_displaced = True
            raise _PreserveTemporaryConfigError(
                f"无法恢复并发更新的 {display_name}；更新保留在 {displaced}"
            ) from exc
        except OSError as exc:
            preserve_displaced = True
            raise _PreserveTemporaryConfigError(
                f"无法恢复并发更新的 {display_name}；更新保留在 {displaced}"
            ) from exc
        raise RuntimeError(
            f"{display_name} 在删除期间发生并发写入，已停止删除"
        )
    finally:
        if not preserve_displaced:
            try:
                displaced.unlink(missing_ok=True)
            except OSError:
                # The canonical path is either absent as requested or already
                # contains the restored concurrent update. A stale private hard
                # link is safer than turning cleanup into destructive recovery.
                pass


def _restore_latest_displaced_file(
    temporary: Path,
    destination: Path,
    *,
    expected_destination: tuple[int, int, bytes],
) -> None:
    for _ in range(32):
        try:
            candidate = _path_fingerprint(temporary)
            _exchange_paths(temporary, destination)
            displaced = _path_fingerprint(temporary)
        except OSError as exc:
            raise _PreserveTemporaryConfigError(
                f"无法恢复并发更新的 {destination.name}；更新保留在 "
                f"{temporary}"
            ) from exc
        if displaced == expected_destination:
            return
        expected_destination = candidate
    raise _PreserveTemporaryConfigError(
        f"{destination.name} 持续发生并发更新；最新置换文件保留在 "
        f"{temporary}"
    )


def _path_fingerprint(path: Path) -> tuple[int, int, bytes]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, path.read_bytes()


def _exchange_paths(first: Path, second: Path) -> bool:
    if sys.platform != "darwin":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        return False
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    at_fdcwd = -2
    rename_swap = 0x00000002
    result = renameatx_np(
        at_fdcwd,
        os.fsencode(first),
        at_fdcwd,
        os.fsencode(second),
        rename_swap,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number))


def _remove_managed_config_block(document: str) -> str:
    lines = document.splitlines(keepends=True)
    outside_string = _toml_lines_outside_multiline_strings(lines)
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if outside_string[index] and lines[index].rstrip("\r\n") == CONFIG_BEGIN:
            end_index = next(
                (
                    candidate
                    for candidate in range(index + 1, len(lines))
                    if outside_string[candidate]
                    and lines[candidate].rstrip("\r\n") == CONFIG_END
                ),
                None,
            )
            if end_index is not None:
                managed_body = "".join(lines[index + 1 : end_index])
                try:
                    parsed = tomllib.loads(managed_body)
                except tomllib.TOMLDecodeError:
                    parsed = {}
                notify = parsed.get("notify")
                if (
                    set(parsed) == {"notify"}
                    and isinstance(notify, list)
                    and notify
                    and notify[-1] == "notify"
                ):
                    index = end_index + 1
                    continue
        kept.append(lines[index])
        index += 1
    return "".join(kept)


def _toml_lines_outside_multiline_strings(lines: list[str]) -> list[bool]:
    state: str | None = None
    output: list[bool] = []
    for line in lines:
        output.append(state is None)
        index = 0
        while index < len(line):
            if state == "basic":
                if line.startswith('"""', index):
                    state = None
                    index += 3
                elif line[index] == "\\":
                    index += 2
                else:
                    index += 1
                continue
            if state == "literal":
                if line.startswith("'''", index):
                    state = None
                    index += 3
                else:
                    index += 1
                continue
            if line[index] == "#":
                break
            if line.startswith('"""', index):
                state = "basic"
                index += 3
            elif line.startswith("'''", index):
                state = "literal"
                index += 3
            elif line[index] in {'"', "'"}:
                quote = line[index]
                index += 1
                while index < len(line):
                    if quote == '"' and line[index] == "\\":
                        index += 2
                    elif line[index] == quote:
                        index += 1
                        break
                    else:
                        index += 1
            else:
                index += 1
    return output


def _toml_array_depths(lines: list[str]) -> list[int]:
    state: str | None = None
    depth = 0
    output: list[int] = []
    for line in lines:
        output.append(depth)
        index = 0
        while index < len(line):
            if state == "basic":
                if line.startswith('"""', index):
                    state = None
                    index += 3
                elif line[index] == "\\":
                    index += 2
                else:
                    index += 1
                continue
            if state == "literal":
                if line.startswith("'''", index):
                    state = None
                    index += 3
                else:
                    index += 1
                continue
            if line[index] == "#":
                break
            if line.startswith('"""', index):
                state = "basic"
                index += 3
            elif line.startswith("'''", index):
                state = "literal"
                index += 3
            elif line[index] in {'"', "'"}:
                quote = line[index]
                index += 1
                while index < len(line):
                    if quote == '"' and line[index] == "\\":
                        index += 2
                    elif line[index] == quote:
                        index += 1
                        break
                    else:
                        index += 1
            elif line[index] == "[":
                depth += 1
                index += 1
            elif line[index] == "]":
                depth = max(0, depth - 1)
                index += 1
            else:
                index += 1
    return output
