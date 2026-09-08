from __future__ import annotations

import asyncio

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import RichLog, Static

from .supervisor import RuntimeSnapshot, RuntimeSupervisor


STATE_COLORS = {
    "starting": "yellow",
    "ready": "green",
    "busy": "cyan",
    "stopping": "yellow",
    "stopped": "bright_black",
    "degraded": "orange1",
    "error": "red",
}


def _format_bytes(value: int) -> str:
    amount = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or suffix == "GiB":
            return f"{amount:.0f} {suffix}" if suffix == "B" else f"{amount:.1f} {suffix}"
        amount /= 1024
    return f"{amount:.1f} GiB"


class ProxyDashboard(App[None]):
    TITLE = "AI Proxy Dashboard"
    SUB_TITLE = "local job and inference scheduler"
    BINDINGS = [
        Binding("s", "stop_proxy", "Stop", show=True),
        Binding("r", "restart_proxy", "Restart", show=True),
        Binding("pageup", "log_page_up", "Scroll log", show=False),
        Binding("pagedown", "log_page_down", "Scroll log", show=False),
        Binding("end", "follow_log", "Follow log", show=True),
        Binding("q", "quit_proxy", "Exit", show=True),
        Binding("ctrl+c", "quit_proxy", "Exit", show=False, priority=True),
    ]
    CSS = """
    Screen {
        background: #0d1117;
        color: #d8dee9;
        layout: vertical;
    }
    #title {
        height: 3;
        padding: 0 2;
        content-align: left middle;
        background: #161b22;
        color: #e6edf3;
        text-style: bold;
    }
    #summary {
        height: 11;
        min-height: 9;
        padding: 0 1;
    }
    .card {
        width: 1fr;
        height: 100%;
        margin: 0 1;
        padding: 0 1;
        border: round #30363d;
        background: #161b22;
    }
    .state-starting, .state-stopping { border: round yellow; }
    .state-ready { border: round green; }
    .state-busy { border: round cyan; }
    .state-degraded { border: round orange; }
    .state-error { border: round red; }
    .state-stopped { border: round #484f58; }
    #log-title {
        height: 1;
        margin: 0 2;
        color: #8b949e;
        text-style: bold;
    }
    #log {
        height: 1fr;
        margin: 0 2;
        padding: 0 1;
        border: round #30363d;
        background: #010409;
        scrollbar-color: #484f58;
    }
    #shortcuts {
        height: 2;
        padding: 0 2;
        content-align: center middle;
        background: #161b22;
        color: #8b949e;
    }
    """

    def __init__(self, supervisor: RuntimeSupervisor) -> None:
        super().__init__()
        self.supervisor = supervisor
        self._last_log_sequence = 0
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Static("AI PROXY  •  initializing", id="title")
        with Horizontal(id="summary"):
            yield Static("[bold]STATUS[/bold]\nStopped", classes="card state-stopped", id="state")
            yield Static("[bold]LISTENERS[/bold]\nNone configured", classes="card", id="listeners")
            yield Static("[bold]QUEUES[/bold]\nLoading…", classes="card", id="queues")
        yield Static("ACTIVITY LOG", id="log-title")
        yield RichLog(
            id="log", wrap=True, highlight=False, markup=False, auto_scroll=True, max_lines=2_000
        )
        yield Static(
            "[S] Stop    [R] Restart    [PgUp/PgDn] Scroll    [End] Follow    [Q / Ctrl+C] Exit",
            id="shortcuts",
        )

    async def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh_snapshot)
        asyncio.create_task(self._initial_start(), name="dashboard-start")

    async def _initial_start(self) -> None:
        await self.supervisor.start()
        await self.refresh_snapshot()

    async def on_unmount(self) -> None:
        if self.supervisor.state != "stopped":
            await self.supervisor.stop()

    async def refresh_snapshot(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            snapshot = await self.supervisor.snapshot()
            self._render_snapshot(snapshot)
        finally:
            self._refreshing = False

    def _render_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        color = STATE_COLORS.get(snapshot.state, "white")
        state_widget = self.query_one("#state", Static)
        for name in STATE_COLORS:
            state_widget.set_class(name == snapshot.state, f"state-{name}")
        state_lines = [
            "[bold]STATUS[/bold]",
            f"[bold {color}]{snapshot.state.upper()}[/bold {color}]",
        ]
        if snapshot.active_backend:
            state_lines.append(f"Backend: {escape(snapshot.active_backend)}")
        if snapshot.active_job:
            state_lines.append(f"Job: {escape(snapshot.active_job)}")
        if snapshot.backend_status:
            backend_state = str(snapshot.backend_status.get("state") or "unknown")
            ownership = str(snapshot.backend_status.get("ownership") or "unknown")
            ownership_label = "managed" if ownership == "proxy" else ownership
            state_lines.append(
                f"ComfyUI backend: {escape(backend_state)} ({escape(ownership_label)})"
            )
        if snapshot.error:
            state_lines.append(f"[red]{escape(snapshot.error)}[/red]")
        elif snapshot.filesystem_degraded:
            state_lines.append(f"[orange1]{escape(snapshot.filesystem_degraded)}[/orange1]")
        elif snapshot.backend_status and snapshot.backend_status.get("blocked_reason"):
            state_lines.append(
                f"[orange1]{escape(str(snapshot.backend_status['blocked_reason']))}[/orange1]"
            )
        elif snapshot.cache_status and snapshot.cache_status.get("error"):
            state_lines.append(
                f"[orange1]Cache: {escape(str(snapshot.cache_status['error']))}[/orange1]"
            )
        state_widget.update("\n".join(state_lines))

        if snapshot.listeners:
            listener_lines = ["[bold]PROXY LISTENERS[/bold]"]
            for listener in snapshot.listeners:
                profile_label = "ComfyUI" if listener.profile == "comfyui" else listener.profile.title()
                listener_color = "green" if listener.state == "listening" else (
                    "yellow" if listener.state == "starting" else "red" if listener.state == "error" else "bright_black"
                )
                listener_lines.append(
                    f"{escape(profile_label)} proxy  {escape(listener.host)}:{listener.port}  "
                    f"[{listener_color}]{listener.state.upper()}[/{listener_color}]"
                )
            managed = bool(
                snapshot.backend_status
                and snapshot.backend_status.get("ownership") == "proxy"
            )
            has_comfyui_proxy = any(
                listener.profile == "comfyui" for listener in snapshot.listeners
            )
            if managed and not has_comfyui_proxy:
                listener_lines.append("[red]ComfyUI proxy  NOT CONFIGURED[/red]")
            elif managed and snapshot.backend_status:
                backend_state = str(snapshot.backend_status.get("state") or "unknown")
                backend_note = " (on demand)" if backend_state == "stopped" else ""
                backend_color = STATE_COLORS.get(backend_state, "white")
                listener_lines.append(
                    f"ComfyUI backend  [{backend_color}]{escape(backend_state.upper())}[/{backend_color}]"
                    f"{backend_note}"
                )
        else:
            listener_lines = ["[bold]PROXY LISTENERS[/bold]", "Filesystem mode", "No HTTP ports configured"]
        self.query_one("#listeners", Static).update("\n".join(listener_lines))

        count = snapshot.filesystem_counts
        queue_lines = [
            "[bold]QUEUES[/bold]",
            f"Filesystem  queued [bold]{count['queued']}[/bold]  running [bold]{count['running']}[/bold]",
            f"HTTP  queued [bold]{snapshot.http_queued}[/bold]  admitted {snapshot.admitted_requests}",
            f"Oldest wait  {snapshot.http_oldest_wait_seconds:.1f}s  buffered {_format_bytes(snapshot.buffered_body_bytes)}",
            f"Answers  [green]✓ {count['succeeded']}[/green]  [red]✕ {count['failed']}[/red]  [yellow]! {count['invalid']}[/yellow]",
        ]
        self.query_one("#queues", Static).update("\n".join(queue_lines))
        self.query_one("#title", Static).update(
            f"AI PROXY  •  [{color}]{snapshot.state.upper()}[/{color}]"
        )

        log = self.query_one("#log", RichLog)
        for entry in snapshot.logs:
            if entry.sequence <= self._last_log_sequence:
                continue
            line = Text()
            line.append(entry.timestamp.strftime("%H:%M:%S"), style="bright_black")
            line.append(f"  {entry.source:<11}", style="blue")
            level_style = {
                "success": "green",
                "warning": "yellow",
                "error": "red",
            }.get(entry.level, "white")
            line.append(f"  {entry.message}", style=level_style)
            log.write(line)
            self._last_log_sequence = entry.sequence

    def action_stop_proxy(self) -> None:
        asyncio.create_task(self._stop_proxy(), name="dashboard-stop")

    async def _stop_proxy(self) -> None:
        await self.supervisor.stop()
        await self.refresh_snapshot()

    def action_restart_proxy(self) -> None:
        asyncio.create_task(self._restart_proxy(), name="dashboard-restart")

    async def _restart_proxy(self) -> None:
        await self.supervisor.restart()
        await self.refresh_snapshot()

    def action_follow_log(self) -> None:
        log = self.query_one("#log", RichLog)
        log.auto_scroll = True
        log.scroll_end(animate=False)

    def action_log_page_up(self) -> None:
        log = self.query_one("#log", RichLog)
        log.auto_scroll = False
        log.scroll_page_up(animate=False)

    def action_log_page_down(self) -> None:
        log = self.query_one("#log", RichLog)
        log.auto_scroll = False
        log.scroll_page_down(animate=False)

    def action_quit_proxy(self) -> None:
        asyncio.create_task(self._quit_proxy(), name="dashboard-exit")

    async def _quit_proxy(self) -> None:
        if self.supervisor.state != "stopped":
            await self.supervisor.stop()
        self.exit()
