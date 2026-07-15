import asyncio
import sys
import time
import signal
import os
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.align import Align
from rich import box
from rich.text import Text
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
import logging

logging.basicConfig(level=logging.WARNING)
console = Console()

BEIGE = "#D4C4A8"
DARK_BEIGE = "#C4A47A"
LIGHT_BEIGE = "#E8D5B7"
BROWN = "#B8A088"

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def print_front_page():
    clear_screen()
    border = "═" * 50
    top = f"╔{border}╗"
    bottom = f"╚{border}╝"
    content = [
        "  ✦  T E L E G R A M   P U R G E   T O O L  ✦",
        "  ❖  Delete all messages (user account)  ❖",
        "  █  All input is shown - no hiding       █"
    ]
    lines = [top]
    for line in content:
        lines.append(f"║  {line:<46}  ║")
    lines.append(bottom)
    console.print(Align.center("\n".join(lines), style=f"bold {BEIGE}"))
    console.print(Align.center("─" * 50, style=BROWN))

def clean_exit():
    clear_screen()
    console.print(Align.center(Panel(
        "[bold]✧  SESSION TERMINATED  ✧[/bold]\n"
        "  The purge tool has been stopped.\n"
        "  Thank you for using Telegram Purge Tool.",
        border_style=DARK_BEIGE,
        box=box.HEAVY,
        padding=(1, 4)
    )))
    sys.exit(0)

def sigint_handler(sig, frame):
    clean_exit()

signal.signal(signal.SIGINT, sigint_handler)

class PurgeUser:
    def __init__(self, api_id, api_hash, phone):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.client = None
        self.entity = None
        self.running = False
        self.paused = False
        self.stop_requested = False
        self.total_messages = 0
        self.deleted = 0
        self.start_time = None
        self.last_update_time = None
        self.recent_activity = []
        self._scanning = False

    async def connect(self):
        session_file = 'user_session.session'
        if os.path.exists(session_file):
            console.log("[yellow]Removing old session (to avoid conflicts).[/yellow]")
            os.remove(session_file)

        self.client = TelegramClient('user_session', self.api_id, self.api_hash)
        await self.client.connect()

        if not await self.client.is_user_authorized():
            console.log("[cyan]Sending login code to your Telegram app/SMS...[/cyan]")
            try:
                await self.client.send_code_request(self.phone)
            except FloodWaitError as e:
                console.log(f"[red]Rate limit: Wait {e.seconds} seconds.[/red]")
                sys.exit(1)
            except PhoneCodeExpiredError:
                console.log("[red]Code expired. Restart and try again.[/red]")
                sys.exit(1)
            except Exception as e:
                err = str(e).lower()
                if 'resend' in err or 'flood' in err:
                    console.log("[red]Too many code requests. Wait 5 minutes and try again.[/red]")
                else:
                    console.log(f"[red]Unexpected error sending code: {e}[/red]")
                sys.exit(1)

            code = Prompt.ask("[cyan]Enter the code (visible in Telegram app / SMS)[/cyan]")
            try:
                await self.client.sign_in(self.phone, code)
            except PhoneCodeInvalidError:
                console.log("[red]Invalid code. Restart.[/red]")
                sys.exit(1)
            except FloodWaitError as e:
                console.log(f"[red]Rate limit: Wait {e.seconds} seconds.[/red]")
                sys.exit(1)
            except SessionPasswordNeededError:
                password = Prompt.ask("[cyan]Enter your 2FA password[/cyan]")
                try:
                    await self.client.sign_in(password=password)
                except Exception as e:
                    console.log(f"[red]2FA login failed: {e}[/red]")
                    sys.exit(1)
            except Exception as e:
                err = str(e).lower()
                if 'resend' in err or 'flood' in err:
                    console.log("[red]Too many attempts. Wait 5 minutes.[/red]")
                else:
                    console.log(f"[red]Login error: {e}[/red]")
                sys.exit(1)

        me = await self.client.get_me()
        console.log(f"[green]Logged in as: {me.first_name} (@{me.username})[/green]")

    async def get_channel(self, identifier):
        try:
            self.entity = await self.client.get_entity(identifier)
            console.log(f"[cyan]Channel: {self.entity.title} (ID: {self.entity.id})[/cyan]")
            return True
        except ValueError:
            console.log(f"[red]Channel not found: {identifier}[/red]")
            return False

    def _format_time(self, seconds):
        if seconds < 60:
            return f"{seconds:.0f}s"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"

    def _progress_bar(self, current, total, width=30):
        if total == 0:
            return "█" * width
        filled = int((current / total) * width)
        return f"[{BEIGE}]{'█' * filled}[/{BEIGE}]" + f"[{BROWN}]{'░' * (width - filled)}[/{BROWN}]"

    async def _delete_chunk(self, ids):
        try:
            await self.client.delete_messages(self.entity, ids)
            return len(ids)
        except FloodWaitError as e:
            self.recent_activity.append(("FloodWait", f"Waiting {e.seconds}s"))
            await asyncio.sleep(e.seconds + 1)
            return 0
        except Exception as e:
            self.recent_activity.append(("Error", f"Delete chunk: {str(e)[:50]}"))
            return 0

    async def _purge_loop(self):
        self.running = True
        self.paused = False
        self.stop_requested = False
        self.deleted = 0
        self.start_time = time.time()
        self.last_update_time = time.time()
        self.recent_activity = []
        self._scanning = True

        self.recent_activity.append(("Info", "Fetching messages..."))
        all_ids = []
        try:
            async for msg in self.client.iter_messages(self.entity, limit=None):
                all_ids.append(msg.id)
        except Exception as e:
            err_msg = f"Scan error: {str(e)[:60]}"
            self.recent_activity.append(("Error", err_msg))
            console.log(f"[red]{err_msg}[/red]")
            self.running = False
            self._scanning = False
            return

        self.total_messages = len(all_ids)
        self._scanning = False
        if self.total_messages == 0:
            self.recent_activity.append(("Info", "No messages found."))
            self.running = False
            return

        self.recent_activity.append(("Info", f"Found {self.total_messages} messages."))
        chunk_size = 100
        for i in range(0, len(all_ids), chunk_size):
            while self.paused and not self.stop_requested:
                await asyncio.sleep(0.5)
            if self.stop_requested:
                break
            chunk = all_ids[i:i+chunk_size]
            if not chunk:
                break
            d = await self._delete_chunk(chunk)
            self.deleted += d
            if d > 0:
                self.recent_activity.append(("Deleted", f"{d} messages"))
                if len(self.recent_activity) > 10:
                    self.recent_activity.pop(0)
            if d == 0:
                await asyncio.sleep(1)
            if time.time() - self.last_update_time >= 1:
                self.last_update_time = time.time()

        self.running = False
        self.recent_activity.append(("Info", "Purge complete."))

    def render_dashboard(self):
        if not self.entity:
            return Panel("[dim]Waiting for channel...[/dim]", border_style=BROWN, box=box.HEAVY)

        elapsed = time.time() - self.start_time if self.start_time else 0
        remaining = self.total_messages - self.deleted
        speed = self.deleted / elapsed if elapsed > 0 else 0
        eta = remaining / speed if speed > 0 else 0
        percentage = (self.deleted / self.total_messages * 100) if self.total_messages else 0
        bar = self._progress_bar(self.deleted, self.total_messages, 30)

        status_text = (
            "▶ Scanning..." if self._scanning else
            "▶ Running" if self.running and not self.paused else
            "⏸ Paused" if self.paused else
            "⏹ Stopped"
        )

        header = Panel(
            Align.center(Text("TELEGRAM PURGE TOOL", style=f"bold {BEIGE}")),
            border_style=DARK_BEIGE,
            box=box.HEAVY
        )

        status_panel = Panel(
            Align.center(f"Channel: [bold]{self.entity.title}[/bold]\nStatus: {status_text}"),
            border_style=BROWN,
            box=box.HEAVY,
            title="Status"
        )

        progress_panel = Panel(
            Align.center(f"{bar}\n{percentage:.1f}%"),
            border_style=DARK_BEIGE,
            box=box.HEAVY,
            title="Progress"
        )

        stats_table = Table(show_header=False, box=None, padding=(0, 2))
        stats_table.add_column(style=BEIGE, width=14)
        stats_table.add_column(style="white", width=12)
        stats_table.add_row("Total", f"{self.total_messages:,}")
        stats_table.add_row("Deleted", f"{self.deleted:,}")
        stats_table.add_row("Remaining", f"{remaining:,}")
        stats_table.add_row("Speed", f"{speed:.1f} msg/s")
        stats_table.add_row("Elapsed", self._format_time(elapsed))
        stats_table.add_row("ETA", self._format_time(eta))

        stats_panel = Panel(
            stats_table,
            border_style=DARK_BEIGE,
            box=box.HEAVY,
            title="Statistics"
        )

        log_table = Table(box=box.HEAVY, border_style=BROWN)
        log_table.add_column("Time", style=BEIGE, width=8)
        log_table.add_column("Status", style="white", width=10)
        log_table.add_column("Message", style="white")
        if self.recent_activity:
            for item in self.recent_activity[-8:]:
                if isinstance(item, tuple):
                    status, msg = item
                    color = "green" if status == "Deleted" else "yellow" if status == "Info" else "red"
                    log_table.add_row(
                        time.strftime("%H:%M:%S"),
                        f"[{color}]{status}[/{color}]",
                        msg
                    )
                else:
                    log_table.add_row(time.strftime("%H:%M:%S"), "[dim]Info[/dim]", str(item))
        else:
            log_table.add_row("--:--:--", "[dim]Idle[/dim]", "Waiting for action...")

        log_panel = Panel(
            log_table,
            border_style=DARK_BEIGE,
            box=box.HEAVY,
            title="Debug Log"
        )

        controls = Panel(
            Align.center("Press Ctrl+C to stop the purge at any time."),
            border_style=DARK_BEIGE,
            box=box.HEAVY,
            title="Controls"
        )

        layout = Group(
            header,
            status_panel,
            progress_panel,
            stats_panel,
            log_panel,
            controls
        )

        return Align.center(Panel(layout, border_style=BEIGE, box=box.HEAVY, padding=(1, 2)))

    async def run(self):
        console.log("[cyan]Starting purge. Press Ctrl+C to stop.[/cyan]")
        await self._purge_loop()

async def main():
    print_front_page()

    console.print("[yellow]Get your api_id and api_hash from https://my.telegram.org[/yellow]")
    api_id = Prompt.ask("[cyan]api_id[/cyan]")
    api_hash = Prompt.ask("[cyan]api_hash[/cyan]")
    phone = Prompt.ask("[cyan]Phone number (with country code, e.g., +639123456789)[/cyan]")

    bot = PurgeUser(int(api_id), api_hash, phone)

    try:
        await bot.connect()
    except Exception as e:
        console.print(f"[red]Connection error: {e}[/red]")
        return

    channel = Prompt.ask(f"[{BEIGE}]Channel username (e.g., @my_channel) or ID[/{BEIGE}]")
    if not await bot.get_channel(channel):
        return

    console.log("[green]Press Enter to start purging all messages.[/green]")
    input()

    with Live(console=console, refresh_per_second=2) as live:
        task = asyncio.create_task(bot.run())
        while not task.done():
            live.update(bot.render_dashboard())
            await asyncio.sleep(0.5)
        await task
        live.update(bot.render_dashboard())
        await asyncio.sleep(1)

    console.log(Panel("[bold green]Purge complete.[/bold green]", title="Done", border_style="green"))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        clean_exit()