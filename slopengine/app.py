"""Keyboard-first Textual presentation; no media dependencies imported here."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, Collapsible, DataTable, Footer, Header, Input, Label,
    RichLog, Select, Static, TabbedContent, TabPane, TextArea,
)

from .auto_mode import AutoMode
from .jobs import JobManager
from .config_core import gemini_key_is_configured
from .library import discover_outputs, open_output
from .settings import SettingsStore, build_config, redact, state_dir, validate_config
from .state import JobState, STAGES

# IDs, labels, and conversion types are shared by Create and Settings.
FIELDS = {
    "edge_voice": ("TTS voice (blank = automatic language selection)", str),
    "edge_rate": ("Speech rate (e.g. +7%)", str),
    "edge_pitch": ("Speech pitch (e.g. -3Hz)", str),
    "edge_volume": ("Speech volume (e.g. +0%)", str),
    "whisper_model": ("Whisper model", str),
    "whisper_device": ("Whisper device (cpu or cuda)", str),
    "background_music_path": ("Background music file (optional)", str),
    "background_music_volume": ("Music volume (0–1)", float),
}
SETTINGS_FIELDS = {
    "gemini_model": ("Gemini model", str),
    **FIELDS,
    "output_dir": ("Output directory", str),
    "caption_font_path": ("Caption font (.ttf / .otf, blank = system)", str),
    "caption_font_size": ("Caption font size", int),
    "caption_y_ratio": ("Caption vertical position (0–1)", float),
    "caption_stroke_width": ("Caption stroke width", int),
    "youtube_privacy_status": ("YouTube privacy (private, unlisted, public)", str),
}


class ConfirmExit(ModalScreen[bool]):
    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static("Jobs are active or queued. Stop active workers and exit? Pending jobs stay saved and require recovery after restart.")
            with Horizontal(classes="actions"):
                yield Button("Keep working", id="stay", variant="primary")
                yield Button("Cancel and exit", id="leave", variant="error")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "leave")


class SlopEngineApp(App):
    TITLE = "SlopEngine"
    SUB_TITLE = "Video generation workspace"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("ctrl+g", "generate", "Generate"),
        Binding("ctrl+x", "cancel_job", "Cancel job"),
        Binding("ctrl+1", "navigate('dashboard')", "Dashboard"),
        Binding("ctrl+2", "navigate('create')", "Create"),
        Binding("ctrl+3", "navigate('library')", "Library"),
        Binding("ctrl+4", "navigate('settings')", "Settings"),
        Binding("ctrl+5", "navigate('logs')", "Logs"),
        Binding("ctrl+6", "navigate('monitor')", "Monitor"),
        Binding("ctrl+7", "navigate('queue')", "Queue"),
        Binding("ctrl+8", "navigate('auto')", "Create · Auto"),
        Binding("p", "auto_pause", "Pause Auto", show=False),
        Binding("r", "auto_resume", "Resume Auto", show=False),
        Binding("s", "auto_stop", "Stop Auto", show=False),
        Binding("x", "auto_cancel", "Cancel Auto", show=False),
        Binding("question_mark", "auto_help", "Auto help", show=False),
        Binding("ctrl+r", "retry_job", "Retry selected", show=False),
        Binding("alt+up", "move_job(-1)", "Move up", show=False),
        Binding("alt+down", "move_job(1)", "Move down", show=False),
        Binding("ctrl+q", "request_quit", "Quit", priority=True),
        Binding("ctrl+c", "request_quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, *, settings_store=None, jobs=None):
        super().__init__()
        self.store = settings_store or SettingsStore()
        self.preferences = {}
        self.startup_error = ""
        try:
            self.preferences = self.store.load()
            self.config = build_config(self.preferences)
        except (ValueError, OSError, RuntimeError, TypeError) as exc:
            self.startup_error = redact(f"Configuration needs attention: {exc}")
            # Invalid environment values must not prevent the interface opening.
            from .config_core import PipelineConfig
            self.config = PipelineConfig(gemini_api_key="", pexels_api_key="")
        self.jobs = jobs or JobManager()
        self.auto_mode = AutoMode(self.jobs, preferences=self.preferences) if hasattr(self.jobs, "auto_sessions") else None
        self._poll_busy = False
        self._auto_event_seq = 0
        self._auto_display_session = None
        self._auto_topic_snapshot = []
        self.job = JobState()
        self.entries = []
        self.selected_entry = None
        self.selected_job_id = None
        self.job_states = {}
        self.queue_rows = []

    def fields(self, prefix: str, definitions: dict) -> ComposeResult:
        values = asdict(self.config)
        for name, (label, _) in definitions.items():
            yield Label(label)
            value = values.get(name)
            if name == "edge_voice" and not self.config.tts_voice_overridden:
                value = ""
            yield Input(str(value) if value is not None else "", id=f"{prefix}-{name}")

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard", id="pages"):
            with TabPane("Dashboard", id="dashboard"):
                with VerticalScroll():
                    yield Static("YOUR VIDEO WORKSPACE", classes="heading")
                    with Vertical(id="gemini-setup", classes="panel"):
                        yield Label("Gemini API key is missing")
                        yield Static("Enter your Google AI Studio key. It is masked and used for this session only.", markup=False)
                        yield Input(password=True, placeholder="Gemini API key", id="gemini-api-key")
                        yield Static(id="gemini-key-error", classes="message", markup=False)
                        yield Button("Use Gemini key", id="use-gemini-key", variant="primary")
                    yield Static(id="readiness", classes="panel", markup=False)
                    yield Static(self.startup_error, id="startup-error", classes="message", markup=False)
                    yield Static(id="dashboard-job", classes="panel", markup=False)
                    yield Button("Create a video", id="new-video", variant="primary")
                    yield Button("View generation", id="view-monitor")
            with TabPane("Create", id="create"):
                with VerticalScroll():
                    yield Label("Generation mode")
                    yield Select((("Manual video", "manual"), ("Auto Mode", "auto")),
                                 value="manual", allow_blank=False, id="create-mode")
                    with Vertical(id="manual-create-fields"):
                        yield Label("Topic or detailed generation prompt *")
                        yield TextArea(id="topic", soft_wrap=True)
                        yield Label("Output filename or path (blank = automatic)")
                        yield Input(placeholder="my-video.mp4", id="output")
                    with Vertical(id="auto-panel"):
                        with Collapsible(title="Session setup · niche and target", collapsed=False, id="auto-setup"):
                            yield Label("Niche or channel focus")
                            yield Input(placeholder="e.g. overlooked engineering failures", id="auto-niche")
                            yield Label("Number of Videos (blank = unlimited)")
                            yield Input(placeholder="blank = unlimited", id="auto-count")
                        yield Static("Duplicate checks use normalized text and local concept rules; history is never silently edited.", id="auto-duplicate-mode", markup=False)
                    with Collapsible(title="Voice, transcription and music", collapsed=True):
                        yield from self.fields("job", FIELDS)
                        yield Checkbox("Keep temporary media", value=self.config.keep_temp, id="keep-temp")
                    yield Checkbox("I consent to upload", id="upload")
                    yield Static(id="upload-description", markup=False)
                    yield Static(id="summary", classes="panel", markup=False)
                    with Vertical(id="manual-actions"):
                        yield Static(id="form-error", classes="message", markup=False)
                        yield Button("Generate video", id="generate", variant="primary")
                    with Vertical(id="auto-actions-panel"):
                        with Horizontal(classes="actions auto-actions"):
                            yield Button("Start Auto", id="auto-start", variant="primary")
                            yield Button("Pause", id="auto-pause")
                            yield Button("Resume", id="auto-resume")
                        with Horizontal(classes="actions auto-actions"):
                            yield Button("Stop after active", id="auto-stop")
                            yield Button("Cancel session", id="auto-cancel", variant="error")
                        yield Static(id="auto-stats", classes="panel", markup=False)
                        yield Static(id="auto-status", classes="message", markup=False)
                        yield Static(id="auto-form-error", classes="message", markup=False)
                        yield Static("P pause · R resume · S stop after active · X cancel this session · ? help", markup=False)
                        yield DataTable(id="auto-table", cursor_type="row")
                        yield Label("Auto session event log (saved in the jobs database)")
                        yield RichLog(id="auto-log", wrap=True, markup=False, max_lines=3000)
            with TabPane("Monitor", id="monitor"):
                with VerticalScroll():
                    yield Static("No generation started", id="job-status", classes="heading", markup=False)
                    yield Static(id="stage-list", classes="panel", markup=False)
                    yield Static(id="job-detail", classes="message", markup=False)
                    yield Button("Cancel generation", id="cancel", variant="error", disabled=True)
                    yield RichLog(id="monitor-log", wrap=True, markup=False, max_lines=2000)
            with TabPane("Queue", id="queue"):
                with VerticalScroll():
                    yield Static("GENERATION QUEUE · Enter: inspect · Ctrl+X: cancel · Ctrl+R: retry · Alt+↑/↓: reorder", classes="heading", markup=False)
                    yield DataTable(id="queue-table", cursor_type="row")
                    yield Static("Select a job to inspect its status and independent log.", id="queue-detail", classes="panel", markup=False)
                    with Horizontal(classes="actions"):
                        yield Button("Add job", id="queue-add", variant="primary")
                        yield Button("Inspect", id="queue-inspect")
                        yield Button("Cancel", id="queue-cancel", variant="error")
                    with Horizontal(classes="actions"):
                        yield Button("Retry / recover", id="queue-retry")
                        yield Button("Move up", id="queue-up")
                        yield Button("Move down", id="queue-down")
                    with Collapsible(title="Bulk add topics", collapsed=True):
                        yield Label("One topic per line. Uses current Create options; YouTube upload is off.")
                        yield TextArea(id="bulk-topics", soft_wrap=True)
                        yield Button("Add topics", id="bulk-add", variant="primary")
                    with Collapsible(title="Concurrency limits", collapsed=True):
                        yield Label("Active jobs")
                        yield Input("2", id="limit-active", type="integer")
                        yield Label("Concurrent renders (production default: 1)")
                        yield Input("1", id="limit-render", type="integer")
                        yield Label("Concurrent GPU transcriptions")
                        yield Input("1", id="limit-gpu", type="integer")
                        yield Button("Apply limits", id="apply-limits")
                    yield Static(id="queue-message", classes="message", markup=False)
                    yield RichLog(id="queue-log", wrap=True, markup=False, max_lines=2000)
            with TabPane("Library", id="library"):
                with VerticalScroll():
                    yield Button("Refresh outputs", id="refresh-library")
                    yield DataTable(id="library-table", cursor_type="row")
                    yield Static("Select a video to inspect its metadata.", id="library-path", markup=False)
                    with Horizontal(classes="actions"):
                        yield Button("Open video", id="open-video")
                        yield Button("Copy path", id="copy-path")
                        yield Button("Reuse topic", id="reuse-topic")
                    yield RichLog(id="metadata", wrap=True, markup=False)
            with TabPane("Settings", id="settings"):
                with VerticalScroll():
                    yield Static("Preferences override .env defaults. Create options apply to one job.\nAPI keys stay in the environment or .env; see .env.example.", classes="panel", markup=False)
                    yield from self.fields("setting", SETTINGS_FIELDS)
                    yield Static(id="settings-message", classes="message", markup=False)
                    yield Button("Save preferences", id="save-settings", variant="primary")
            with TabPane("Logs", id="logs"):
                with VerticalScroll():
                    yield Static("Saved job logs (credentials are redacted)", classes="heading")
                    yield Select([], prompt="Choose a previous job", id="log-select")
                    yield Button("Refresh logs", id="refresh-logs")
                    yield RichLog(id="log-view", wrap=True, markup=False, max_lines=3000)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#library-table", DataTable).add_columns("Title", "Topic", "Created", "Filename", "Size", "YouTube")
        self.query_one("#queue-table", DataTable).add_columns("ID", "Topic", "Stage", "Status / waiting reason")
        self.query_one("#auto-table", DataTable).add_columns("Topic", "Status", "Stage", "Progress")
        limits = getattr(self.jobs, "limits", None)
        for key, field in [("max_active_jobs", "active"), ("max_concurrent_renders", "render"), ("max_concurrent_gpu_whisper", "gpu")]:
            if limits is not None:
                self.query_one(f"#limit-{field}", Input).value = str(getattr(limits, key))
        if self.auto_mode and self.auto_mode.session_id:
            self.query_one("#auto-niche", Input).value = self.auto_mode.niche
            self.query_one("#auto-count", Input).value = str(self.auto_mode.target or "")
            if self.auto_mode.state not in {"STOPPED", "COMPLETED"}:
                self.query_one("#create-mode", Select).value = "auto"
        self.update_create_mode()
        self.refresh_queue()
        self.refresh_library()
        self.refresh_logs()
        self.update_summary()
        self.update_monitor()
        self.refresh_dashboard()
        self.refresh_auto()
        self.poll_timer = self.set_interval(0.2, self.poll_background)

    def action_navigate(self, page: str) -> None:
        if page == "auto":
            page = "create"
            self.query_one("#create-mode", Select).value = "auto"
            self.update_create_mode()
        elif page == "create":
            self.query_one("#create-mode", Select).value = "manual"
            self.update_create_mode()
        self.query_one("#pages", TabbedContent).active = page
        if page == "dashboard":
            self.query_one("#new-video", Button).focus()
            self.refresh_dashboard()
        elif page == "create":
            focus = "#auto-niche" if self.create_mode() == "auto" else "#topic"
            self.query_one(focus).focus()
        elif page == "library":
            self.refresh_library()
            self.query_one("#library-table", DataTable).focus()
        elif page == "logs":
            self.refresh_logs()
            self.query_one("#log-select", Select).focus()
        elif page == "queue":
            self.refresh_queue()
            self.query_one("#queue-table", DataTable).focus()
        elif page == "monitor":
            self.query_one("#monitor-log", RichLog).focus()
        elif page == "settings":
            self.query_one("#setting-gemini_model", Input).focus()

    def create_mode(self) -> str:
        return str(self.query_one("#create-mode", Select).value or "manual")

    def update_create_mode(self) -> None:
        if not self.is_mounted or not self.query("#create-mode"):
            return
        automatic = self.create_mode() == "auto"
        self.query_one("#manual-create-fields").display = not automatic
        self.query_one("#auto-panel").display = automatic
        self.query_one("#manual-actions").display = not automatic
        self.query_one("#auto-actions-panel").display = automatic
        self.update_upload_description()
        self.update_summary()

    def update_upload_description(self, snapshot=None) -> None:
        if not self.is_mounted or not self.query("#upload-description"):
            return
        automatic = self.create_mode() == "auto"
        if automatic and snapshot and snapshot.get("id"):
            enabled = snapshot.get("upload_enabled", False)
            privacy = snapshot.get("upload_privacy", "private")
            text = (f"This Auto session captured upload consent {'ON' if enabled else 'OFF'} · privacy {privacy}. "
                    "The checkbox applies to the next session.")
        elif automatic:
            text = ("Checked at Start: upload every video in this Auto session using the selected privacy setting. "
                    "Unchecked: keep all session videos local.")
        else:
            text = "Checked: upload this manual video after it is saved. Unchecked: keep it local."
        self.query_one("#upload-description", Static).update(text)

    def refresh_dashboard(self) -> None:
        dependencies = []
        for name in ("google.genai", "edge_tts", "whisper", "moviepy", "numpy", "PIL", "requests"):
            try:
                found = importlib.util.find_spec(name) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                dependencies.append(name)
        gemini_ready = gemini_key_is_configured(self.config.gemini_api_key)
        self.query_one("#gemini-setup").display = not gemini_ready
        keys = f"Gemini: {'configured' if gemini_ready else 'missing'} · Pexels: {'configured' if self.config.pexels_api_key else 'missing'}"
        latest = self.entries[0].path if self.entries else "None yet"
        self.query_one("#readiness", Static).update(
            f"API configuration   {keys}\nFFmpeg   {'ready' if shutil.which('ffmpeg') else 'missing — install FFmpeg on PATH'}\n"
            f"Generation engines   {'missing: ' + ', '.join(dependencies) if dependencies else 'installed (loaded only when generating)'}\n"
            f"Completed videos   {len(self.entries)}\nMost recent   {latest}\n"
            "Missing keys? Copy .env.example to .env and configure GEMINI_API_KEY and PEXELS_API_KEY."
        )
        self.query_one("#dashboard-job", Static).update(f"Generation: {self.job.status} · elapsed {self.job.elapsed}s\n{self.job.output or self.job.detail}")

    def refresh_auto(self) -> None:
        if not self.query("#auto-stats"):
            return
        if self.auto_mode is None:
            self.query_one("#auto-stats", Static).update("Auto Mode requires the persistent JobManager.")
            self.query_one("#auto-status", Static).update("Auto controls are unavailable for this job manager.")
            return
        try:
            snapshot = self.auto_mode.snapshot()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            self.query_one("#auto-status", Static).update(redact(f"Auto Mode database error: {exc}"))
            return
        self.update_upload_description(snapshot)
        target = "unlimited" if snapshot["target"] is None else str(snapshot["target"])
        budget = "unlimited" if snapshot["request_budget"] == 0 else str(snapshot["request_budget"])
        self.query_one("#auto-stats", Static).update(
            f"Niche: {snapshot['niche'] or '—'} · Target: {target} · Generated: {snapshot['generated']} · Active: {snapshot['active']} · "
            f"Queued: {snapshot['queued']} · Duplicate rejections: {snapshot['duplicates']} · "
            f"Topic requests: {snapshot['request_count']} / {budget} · {snapshot['requests_per_minute']} per minute · "
            f"YouTube: {'upload enabled' if snapshot['upload_enabled'] else 'local only'} ({snapshot['upload_privacy']})"
        )
        self.update_upload_description(snapshot)
        self.query_one("#auto-status", Static).update(f"{snapshot['state']} · {snapshot['message']}")
        table = self.query_one("#auto-table", DataTable)
        rendered = []
        core_stages = ("script", "narration", "timestamps", "footage", "render", "save")
        for row in snapshot["topics"]:
            if row.get("job_status") not in {"QUEUED", "HELD", "RUNNING", "WAITING", "CANCELLING"}:
                continue
            states = row.get("stage_states") or "{}"
            try:
                states = json.loads(states) if isinstance(states, str) else states
            except ValueError:
                states = {}
            completed = sum(states.get(stage) == "COMPLETED" for stage in core_stages)
            status = row.get("job_status") or row.get("status") or "Reserved"
            stage = row.get("stage") or row.get("waiting_reason") or "Queued"
            rendered.append((row.get("title", ""), str(status), str(stage), f"{completed}/6 stages"))
        if rendered != self._auto_topic_snapshot:
            self._auto_topic_snapshot = rendered
            table.clear()
            for title, status, stage, progress in rendered:
                table.add_row(title, status, stage, progress)
        if snapshot["id"] != self._auto_display_session:
            self._auto_display_session = snapshot["id"]
            self._auto_event_seq = 0
            self.query_one("#auto-log", RichLog).clear()
        if snapshot["id"]:
            for event in self.jobs.auto_events(snapshot["id"], after=self._auto_event_seq):
                self.query_one("#auto-log", RichLog).write(
                    f"{event['time']} {event['level']} [{event['kind']}] {redact(event['message'])}"
                )
                self._auto_event_seq = max(self._auto_event_seq, event["seq"])
        active = snapshot["state"] in {"STARTING", "RUNNING", "PAUSING", "PAUSED", "STOPPING"}
        blocked_error = snapshot["state"] == "ERROR" and bool(snapshot["id"]) and bool(snapshot["active"] or snapshot["queued"])
        self.query_one("#auto-setup", Collapsible).collapsed = active or bool(blocked_error)
        self.query_one("#auto-start", Button).disabled = active or blocked_error
        self.query_one("#auto-pause", Button).disabled = snapshot["state"] != "RUNNING"
        self.query_one("#auto-resume", Button).disabled = snapshot["state"] not in {"PAUSED", "ERROR"} or not snapshot["id"]
        can_stop = snapshot["state"] in {"RUNNING", "PAUSING", "PAUSED"} or (
            snapshot["state"] == "ERROR" and bool(snapshot["id"]) and bool(snapshot["active"] or snapshot["queued"]))
        self.query_one("#auto-stop", Button).disabled = not can_stop
        self.query_one("#auto-cancel", Button).disabled = not can_stop

    def auto_shortcut_allowed(self) -> bool:
        if (not self.is_mounted or self.query_one("#pages", TabbedContent).active != "create"
                or self.create_mode() != "auto"):
            return False
        return not isinstance(self.focused, (Input, TextArea))

    def action_auto_help(self) -> None:
        if not self.auto_shortcut_allowed():
            return
        self.notify("P pauses at stage boundaries; R resumes; S stops after started Auto jobs finish; X cancels only jobs in this Auto session. A blank count runs until stopped. The Create upload checkbox covers the session; topic RPM/budget settings count discovery requests, not script-generation calls.", timeout=8)

    async def action_auto_start(self) -> None:
        if self.auto_mode is None:
            self.query_one("#auto-status", Static).update("Auto Mode requires the persistent JobManager.")
            return
        niche = self.query_one("#auto-niche", Input).value.strip()
        raw_count = self.query_one("#auto-count", Input).value.strip()
        if raw_count and (not raw_count.isdecimal() or int(raw_count) <= 0):
            self.query_one("#auto-form-error", Static).update("Number of Videos must be a positive whole number, or blank for unlimited.")
            return
        target = int(raw_count) if raw_count else None
        try:
            _, config = self.create_config()
            errors = validate_config(config, topic=niche, require_keys=True)
            if errors:
                raise ValueError("\n".join(errors))
            await asyncio.to_thread(self.auto_mode.start, niche, target, config=config)
            # AutoMode already captured the checkbox into the durable session
            # snapshot; a later session must receive fresh explicit consent.
            self.query_one("#upload", Checkbox).value = False
            self.query_one("#auto-form-error", Static).update("")
        except (OSError, ValueError, RuntimeError, TypeError, sqlite3.Error) as exc:
            self.query_one("#auto-form-error", Static).update(redact(exc))
        self.refresh_auto()

    async def action_auto_pause(self) -> None:
        if not self.auto_shortcut_allowed():
            return
        if self.auto_mode:
            await asyncio.to_thread(self.auto_mode.pause)
            self.refresh_auto()

    async def action_auto_resume(self) -> None:
        if not self.auto_shortcut_allowed():
            return
        if self.auto_mode:
            try:
                await asyncio.to_thread(self.auto_mode.resume, config=self.config)
                self.query_one("#upload", Checkbox).value = False
            except (OSError, ValueError, RuntimeError, TypeError, sqlite3.Error) as exc:
                self.query_one("#auto-form-error", Static).update(redact(exc))
            self.refresh_auto()

    async def action_auto_stop(self) -> None:
        if not self.auto_shortcut_allowed():
            return
        if self.auto_mode:
            try:
                await asyncio.to_thread(self.auto_mode.stop)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self.query_one("#auto-form-error", Static).update(redact(exc))
            self.refresh_auto()

    async def action_auto_cancel(self) -> None:
        if not self.auto_shortcut_allowed():
            return
        if self.auto_mode:
            try:
                await asyncio.to_thread(self.auto_mode.stop, cancel=True)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self.query_one("#auto-form-error", Static).update(redact(exc))
            self.refresh_auto()

    @on(Input.Submitted, "#gemini-api-key")
    def use_gemini_key(self) -> None:
        field = self.query_one("#gemini-api-key", Input)
        key = field.value.strip()
        if not gemini_key_is_configured(key) or any(char.isspace() for char in key):
            self.query_one("#gemini-key-error", Static).update("Enter a Gemini API key, rather than the example placeholder, without spaces.")
            return
        # Child workers inherit the session environment; preferences stay non-secret.
        os.environ["GEMINI_API_KEY"] = key
        self.config = replace(self.config, gemini_api_key=key)
        field.value = ""
        self.query_one("#gemini-key-error", Static).update("")
        self.refresh_dashboard()
        self.notify("Gemini key configured for this session.")
        self.query_one("#new-video", Button).focus()

    def read_fields(self, prefix: str, definitions: dict) -> dict:
        result = {}
        for name, (_, convert) in definitions.items():
            value = self.query_one(f"#{prefix}-{name}", Input).value.strip()
            try:
                result[name] = convert(value) if value or convert is not str else ""
            except ValueError:
                raise ValueError(f"{definitions[name][0]} requires a number.") from None
        result["tts_voice_overridden"] = bool(result.get("edge_voice"))
        if not result.get("edge_voice"):
            result["edge_voice"] = self.config.edge_voice
        return result

    def create_config(self):
        overrides = self.read_fields("job", FIELDS)
        overrides["keep_temp"] = self.query_one("#keep-temp", Checkbox).value
        overrides["youtube_upload_enabled"] = self.query_one("#upload", Checkbox).value
        config = build_config(self.preferences, overrides)
        return overrides, config

    def job_inputs(self):
        topic = self.query_one("#topic", TextArea).text.strip()
        overrides, config = self.create_config()
        raw_output = self.query_one("#output", Input).value.strip()
        output = Path(raw_output).expanduser() if raw_output else None
        if output is not None and output.parent == Path("."):
            output = config.output_dir / output
        if output is not None and output.suffix.lower() != ".mp4":
            raise ValueError("Output filename must end in .mp4.")
        return topic, overrides, config, output

    def update_summary(self) -> None:
        if not self.is_mounted or not self.query("#summary"):
            return
        try:
            _, config = self.create_config()
            voice = f"{config.edge_voice if config.tts_voice_overridden else 'automatic by narration language'} · rate {config.edge_rate} · Whisper {config.whisper_model} / {config.whisper_device}"
            if self.create_mode() == "auto":
                niche = self.query_one("#auto-niche", Input).value.strip() or "—"
                target = self.query_one("#auto-count", Input).value.strip() or "unlimited"
                summary = f"Auto niche: {niche} · target: {target}\nVoice: {voice}\nYouTube: {'UPLOAD REQUESTED · ' + config.youtube_privacy_status if config.youtube_upload_enabled else 'local only'}"
            else:
                raw_output = self.query_one("#output", Input).value.strip()
                output = Path(raw_output).expanduser() if raw_output else None
                if output is not None and output.parent == Path("."):
                    output = config.output_dir / output
                if output is not None and output.suffix.lower() != ".mp4":
                    raise ValueError("Output filename must end in .mp4.")
                summary = f"Output: {output or config.output_dir / '<automatic>.mp4'}\nVoice: {voice}\nYouTube: {'UPLOAD REQUESTED · ' + config.youtube_privacy_status if config.youtube_upload_enabled else 'off'}"
        except (ValueError, RuntimeError, TypeError) as exc:
            summary = str(exc)
        self.query_one("#summary", Static).update(summary)

    @on(Input.Changed)
    @on(Checkbox.Changed)
    @on(Select.Changed)
    def form_changed(self, event=None) -> None:
        if isinstance(event, Select.Changed) and event.select.id == "create-mode":
            self.update_create_mode()
            return
        self.update_summary()
        self.update_upload_description()

    async def action_generate(self) -> None:
        if self.create_mode() == "auto":
            await self.action_auto_start()
            return
        if self.jobs.running and not hasattr(self.jobs, "list_jobs"):
            self.notify("A generation is already running. See Monitor.")
            self.action_navigate("monitor")
            return
        try:
            topic, overrides, config, output = self.job_inputs()
            errors = validate_config(config, topic=topic, output=output, require_keys=True)
            if not shutil.which("ffmpeg"):
                errors.append("FFmpeg is missing. Install it and make it available on PATH.")
            if errors:
                raise ValueError("\n".join(errors))
            job_id = self.jobs.start(topic, output, {**self.preferences, **overrides})
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            self.query_one("#form-error", Static).update(redact(exc))
            self.action_navigate("create")
            return
        self.job = JobState()
        self.job.begin(config.youtube_upload_enabled)
        self.selected_job_id = job_id
        if job_id is not None:
            self.job_states[job_id] = self.job
        # Consent belongs to this job; a subsequent job requires another opt-in.
        self.query_one("#upload", Checkbox).value = False
        self.query_one("#monitor-log", RichLog).clear()
        self.query_one("#form-error", Static).update("")
        self.query_one("#generate", Button).disabled = not hasattr(self.jobs, "list_jobs")
        self.query_one("#cancel", Button).disabled = False
        self.action_navigate("monitor")
        self.update_monitor()
        self.refresh_queue()

    def poll_job(self) -> None:
        if not self.query("#job-status"):
            return
        events = self.jobs.poll()
        if self.auto_mode:
            self.auto_mode.tick()
        self.apply_poll_events(events)

    async def poll_background(self) -> None:
        if self._poll_busy or not self.is_mounted:
            return
        self._poll_busy = True
        try:
            events = await asyncio.to_thread(self.jobs.poll)
            if self.auto_mode:
                await asyncio.to_thread(self.auto_mode.tick)
            self.apply_poll_events(events)
        except Exception as exc:
            if self.query("#auto-status"):
                self.query_one("#auto-status", Static).update(redact(f"Background controller error: {type(exc).__name__}: {exc}"))
        finally:
            self._poll_busy = False

    def apply_poll_events(self, events) -> None:
        if not self.query("#job-status"):
            return
        for event in events:
            job_id = event.get("job_id")
            state = self.job_states.setdefault(job_id, JobState()) if job_id else self.job
            state.apply(event)
            line = self.event_line(event)
            if job_id is None or job_id == self.selected_job_id:
                self.job = state
                self.query_one("#monitor-log", RichLog).write(line)
                self.query_one("#queue-log", RichLog).write(line)
            if event.get("type") in {"job_completed", "job_failed", "job_cancelled"}:
                self.query_one("#generate", Button).disabled = False
                if job_id is None or job_id == self.selected_job_id:
                    self.query_one("#cancel", Button).disabled = True
                self.refresh_library()
                self.refresh_logs()
                self.notify(f"Generation {str(job_id or '')[:8]} {state.status.lower()}")
        self.update_monitor()
        self.refresh_queue()
        self.refresh_auto()

    @staticmethod
    def event_line(event: dict) -> str:
        return f"{event.get('time', '')} {event.get('level', 'INFO')} [{event.get('stage') or event.get('type', '')}] {event.get('message', '')} {event.get('output', '')}".rstrip()

    def update_monitor(self) -> None:
        if not self.query("#job-status"):
            return
        current = next((STAGES[key] for key, status in self.job.stages.items() if status == "Running"), "")
        self.query_one("#job-status", Static).update(f"{self.job.status} · {self.job.elapsed}s" + (f" · {current} …" if current else ""))
        self.sub_title = f"{self.job.status}" + (f" · {current}" if current else " · Video generation workspace")
        self.query_one("#stage-list", Static).update("\n".join(f"{i}. {label}   {self.job.stages[key]}" for i, (key, label) in enumerate(STAGES.items(), 1)))
        self.query_one("#job-detail", Static).update("\n".join(filter(None, [self.job.detail, self.job.output])))
        self.query_one("#dashboard-job", Static).update(f"Generation: {self.job.status} · {self.job.elapsed}s\n{self.job.output or self.job.detail}")

    async def action_cancel_job(self) -> None:
        if self.jobs.running:
            if hasattr(self.jobs, "list_jobs") and self.selected_job_id is None:
                self.notify("Select a job in Queue first.")
                return
            self.query_one("#cancel", Button).disabled = True
            self.job.status = "Cancelling"
            if hasattr(self.jobs, "list_jobs"):
                await asyncio.to_thread(self.jobs.cancel, self.selected_job_id)
            else:
                await asyncio.to_thread(self.jobs.cancel)
            self.poll_job()

    def refresh_queue(self) -> None:
        if not hasattr(self.jobs, "list_jobs") or not self.query("#queue-table"):
            return
        rows = self.jobs.list_jobs()
        table = self.query_one("#queue-table", DataTable)
        if rows != self.queue_rows:
            self.queue_rows = rows
            table.clear()
            for row in rows:
                reason = row.get("waiting_reason") or ""
                table.add_row(str(row["id"])[:8], str(row["topic"]), str(row.get("stage") or "Waiting"),
                              f"{row['status']}" + (f" · {reason}" if reason else ""), key=str(row["id"]))
            index = next((i for i, row in enumerate(rows) if row["id"] == self.selected_job_id), None)
            if index is not None:
                table.move_cursor(row=index)
        self.update_queue_detail()

    def update_queue_detail(self) -> None:
        row = next((row for row in self.queue_rows if row["id"] == self.selected_job_id), None)
        if row:
            if "stage_states" in row and self.selected_job_id in self.job_states:
                state = self.job_states[self.selected_job_id]
                state.status = str(row["status"]).replace("_", " ").title()
                state.stages.update({key: str(value).replace("_", " ").title() for key, value in row["stage_states"].items() if key in STAGES})
                state.detail = str(row.get("waiting_reason") or row.get("message") or state.detail)
                state.output = str(row.get("output") or "") if row["status"] == "COMPLETED" else ""
                self.update_monitor()
            self.query_one("#queue-detail", Static).update(redact(
                f"{row['id']} · {row['topic']}\n{row['status']} · {row.get('stage') or 'Waiting'}\n"
                f"{row.get('waiting_reason') or row.get('message') or row.get('error') or ''}\nOutput destination: {row.get('output') or 'Pending'}\nLog: {row.get('log_path') or 'Pending'}"))

    def select_queue_job(self, job_id: str) -> None:
        row = next((row for row in self.queue_rows if str(row["id"]) == job_id), None)
        if row is None:
            return
        changed = self.selected_job_id != row["id"]
        self.selected_job_id = row["id"]
        state = self.job_states.get(self.selected_job_id)
        if state is None:
            state = JobState()
            state.status = str(row["status"]).replace("_", " ").title()
            state.detail = str(row.get("waiting_reason") or row.get("message") or row.get("error") or "")
            state.output = str(row.get("output") or "") if row["status"] == "COMPLETED" else ""
            self.job_states[self.selected_job_id] = state
        self.job = state
        self.query_one("#cancel", Button).disabled = str(row["status"]).lower() in {"completed", "failed", "cancelled", "interrupted"}
        self.update_queue_detail()
        self.update_monitor()
        if changed:
            for selector in ("#queue-log", "#monitor-log"):
                log = self.query_one(selector, RichLog)
                log.clear()
                path = row.get("log_path")
                if path:
                    try:
                        from collections import deque
                        with Path(path).open(encoding="utf-8") as handle:
                            for line in deque(handle, maxlen=2000):
                                try:
                                    log.write(self.event_line(json.loads(redact(line))))
                                except (ValueError, TypeError, AttributeError):
                                    log.write(redact(line.rstrip()))
                    except OSError as exc:
                        log.write(f"Cannot read log: {exc}")

    @on(DataTable.RowHighlighted, "#queue-table")
    def highlight_queue_job(self, event: DataTable.RowHighlighted) -> None:
        self.select_queue_job(str(event.row_key.value))

    @on(DataTable.RowSelected, "#queue-table")
    def inspect_queue_job(self, event: DataTable.RowSelected) -> None:
        self.select_queue_job(str(event.row_key.value))
        self.action_navigate("monitor")

    def action_retry_job(self) -> None:
        if self.selected_job_id is not None and hasattr(self.jobs, "retry"):
            try:
                self.jobs.retry(self.selected_job_id)
                self.refresh_queue()
            except (OSError, RuntimeError, ValueError) as exc:
                self.query_one("#queue-message", Static).update(redact(exc))

    def action_move_job(self, direction: int) -> None:
        if self.selected_job_id is not None and hasattr(self.jobs, "reorder"):
            try:
                self.jobs.reorder(self.selected_job_id, direction)
                self.refresh_queue()
            except (OSError, RuntimeError, ValueError) as exc:
                self.query_one("#queue-message", Static).update(redact(exc))

    def add_bulk_jobs(self) -> None:
        message = self.query_one("#queue-message", Static)
        if not hasattr(self.jobs, "list_jobs"):
            message.update("Bulk generation requires the persistent job manager.")
            return
        self.query_one("#create-mode", Select).value = "manual"
        self.update_create_mode()
        topics = [line.strip() for line in self.query_one("#bulk-topics", TextArea).text.splitlines() if line.strip()]
        try:
            if not topics:
                raise ValueError("Enter one topic per line.")
            _, overrides, _, _ = self.job_inputs()
            overrides["youtube_upload_enabled"] = False
            config = build_config(self.preferences, overrides)
            for topic in topics:
                errors = validate_config(config, topic=topic, require_keys=True)
                if errors:
                    raise ValueError("\n".join(errors))
            if not shutil.which("ffmpeg"):
                raise ValueError("FFmpeg is missing. Install it and make it available on PATH.")
            for topic in topics:
                self.jobs.start(topic, None, {**self.preferences, **overrides})
            self.query_one("#bulk-topics", TextArea).load_text("")
            message.update(f"Added {len(topics)} jobs. Upload is off.")
            self.refresh_queue()
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            message.update(redact(exc))

    def apply_concurrency_limits(self) -> None:
        try:
            limits = {name: int(self.query_one(f"#limit-{field}", Input).value) for name, field in
                      [("max_active_jobs", "active"), ("max_concurrent_renders", "render"), ("max_concurrent_gpu_whisper", "gpu")]}
            if any(value < 1 for value in limits.values()):
                raise ValueError("Concurrency limits must be positive integers.")
            self.jobs.configure(**limits)
            self.query_one("#queue-message", Static).update("Limits applied. Two production renders require a measured benchmark; default is one.")
        except (AttributeError, OSError, ValueError, RuntimeError, TypeError) as exc:
            self.query_one("#queue-message", Static).update(redact(exc))

    def refresh_library(self) -> None:
        self.entries = discover_outputs(self.config.output_dir)
        self.selected_entry = None
        table = self.query_one("#library-table", DataTable)
        table.clear()
        for index, entry in enumerate(self.entries):
            table.add_row(entry.title, entry.topic, str(entry.created)[:19], entry.path.name, f"{entry.size / 1024 / 1024:.1f} MB", entry.youtube, key=str(index))

    @on(DataTable.RowHighlighted, "#library-table")
    def select_output(self, event: DataTable.RowHighlighted) -> None:
        try:
            self.selected_entry = self.entries[int(event.row_key.value)]
        except (IndexError, ValueError, TypeError):
            return
        entry = self.selected_entry
        self.query_one("#library-path", Static).update(str(entry.path.resolve()))
        log = self.query_one("#metadata", RichLog)
        log.clear()
        if entry.metadata_error:
            log.write(entry.metadata_error)
        # Metadata can be user-edited; apply the same redaction as worker logs.
        from .settings import redact
        log.write(redact(json.dumps(entry.metadata, ensure_ascii=False, indent=2)))

    def refresh_logs(self) -> None:
        log_dir = getattr(self.jobs, "log_dir", state_dir() / "logs")
        paths = sorted(Path(log_dir).glob("*.jsonl"), reverse=True)[:100] if Path(log_dir).exists() else []
        self.query_one("#log-select", Select).set_options([(path.name, str(path)) for path in paths])

    @on(Select.Changed, "#log-select")
    def load_log(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        log = self.query_one("#log-view", RichLog)
        log.clear()
        from .settings import redact
        try:
            with Path(str(event.value)).open(encoding="utf-8") as handle:
                from collections import deque
                for line in deque(handle, maxlen=3000):
                    try:
                        log.write(self.event_line(json.loads(redact(line))))
                    except (ValueError, TypeError, AttributeError):
                        log.write(redact(line.rstrip()))
        except OSError as exc:
            log.write(f"Cannot read log: {exc}")

    def save_settings(self) -> None:
        message = self.query_one("#settings-message", Static)
        try:
            preferences = self.read_fields("setting", SETTINGS_FIELDS)
            config = build_config(preferences)
            errors = validate_config(config)
            if errors:
                raise ValueError("\n".join(errors))
            self.store.save(preferences)
            self.preferences, self.config = preferences, config
            # Apply defaults to the next job; active worker has its own snapshot.
            for name in FIELDS:
                value = getattr(config, name)
                if name == "edge_voice" and not config.tts_voice_overridden:
                    value = ""
                self.query_one(f"#job-{name}", Input).value = str(value) if value is not None else ""
            message.update("Preferences saved. Applied to future generations.")
            self.query_one("#startup-error", Static).update("")
            self.refresh_library()
            self.refresh_dashboard()
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            message.update(redact(exc))

    @on(Button.Pressed)
    async def button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "new-video":
            self.action_navigate("create")
            self.query_one("#topic", TextArea).focus()
        elif action == "use-gemini-key":
            self.use_gemini_key()
        elif action == "view-monitor":
            self.action_navigate("monitor")
        elif action == "generate":
            await self.action_generate()
        elif action == "cancel":
            await self.action_cancel_job()
        elif action == "queue-add":
            self.action_navigate("create")
            self.query_one("#topic", TextArea).focus()
        elif action == "queue-inspect":
            self.action_navigate("monitor")
        elif action == "queue-cancel":
            await self.action_cancel_job()
        elif action == "queue-retry":
            self.action_retry_job()
        elif action in {"queue-up", "queue-down"}:
            self.action_move_job(-1 if action == "queue-up" else 1)
        elif action == "bulk-add":
            self.add_bulk_jobs()
        elif action == "apply-limits":
            self.apply_concurrency_limits()
        elif action == "auto-start":
            await self.action_auto_start()
        elif action == "auto-pause":
            await self.action_auto_pause()
        elif action == "auto-resume":
            await self.action_auto_resume()
        elif action == "auto-stop":
            await self.action_auto_stop()
        elif action == "auto-cancel":
            await self.action_auto_cancel()
        elif action == "save-settings":
            self.save_settings()
        elif action == "refresh-library":
            self.refresh_library()
        elif action == "refresh-logs":
            self.refresh_logs()
        elif action in {"open-video", "copy-path", "reuse-topic"}:
            entry = self.selected_entry
            if entry is None:
                self.notify("Select a video first.")
                return
            if action == "reuse-topic":
                self.query_one("#topic", TextArea).load_text(entry.topic or entry.title)
                self.action_navigate("create")
            elif action == "copy-path":
                self.copy_to_clipboard(str(entry.path.resolve()))
                self.notify("Path sent to terminal clipboard. It is also shown above.")
            else:
                try:
                    await asyncio.to_thread(open_output, entry.path)
                except (OSError, RuntimeError, ValueError) as exc:
                    self.notify(str(exc), severity="error")

    async def action_request_quit(self) -> None:
        if self.jobs.running or (self.auto_mode and self.auto_mode.state in {"STARTING", "RUNNING", "PAUSING", "PAUSED", "STOPPING"}):
            self.push_screen(ConfirmExit(), self.confirm_exit)
        else:
            self.exit()

    async def confirm_exit(self, confirmed: bool) -> None:
        if confirmed:
            if self.auto_mode:
                await asyncio.to_thread(self.auto_mode.close)
            await asyncio.to_thread(self.jobs.close)
            self.exit()

    async def on_unmount(self) -> None:
        if hasattr(self, "poll_timer"):
            self.poll_timer.stop()
        if self.auto_mode:
            await asyncio.to_thread(self.auto_mode.close)
        await asyncio.to_thread(self.jobs.close)


def main() -> None:
    app = SlopEngineApp()
    previous_handlers = {}

    def terminate(signum, frame):
        # A terminal hangup must also stop the detached media worker.
        app.exit(return_code=128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous_handlers[sig] = signal.signal(sig, terminate)
    try:
        app.run()
    finally:
        app.jobs.close()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
