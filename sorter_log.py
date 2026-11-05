"""Отдельный журнал сортировщика без перенаправления stdout всего процесса."""

from datetime import datetime
from pathlib import Path
import shlex
import subprocess
import sys
from threading import Lock


def reportMessage(message: str, message_sink=None, *, error: bool = False) -> None:
    if message_sink is not None:
        message_sink(message)
    else:
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


class SorterLog:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.lock = Lock()
        self.write_failed = False

    def write(self, message: str) -> None:
        with self.lock:
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                    for line in message.splitlines():
                        stream.write(f"[{timestamp}] {line}\n")
                self.write_failed = False
            except OSError as error:
                # Ошибка журнала не должна прерывать управление механизмами.
                if not self.write_failed:
                    print(f"Не удалось записать журнал {self.path}: {error}", file=sys.stderr, flush=True)
                self.write_failed = True

    def openWindow(self) -> bool:
        """Открыть tail в новом окне Terminal на macOS; порт Arduino не используется."""
        if sys.platform == "darwin":
            script = '''on run argv
    tell application "Terminal"
        do script ("tail -n 100 -F " & quoted form of (item 1 of argv))
    end tell
end run'''
            try:
                subprocess.run(["osascript", "-e", script, str(self.path)],
                               check=True, capture_output=True, text=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError) as error:
                print(f"Не удалось открыть окно журнала: {error}")
        print("Откройте другой терминал и выполните:")
        print(f"tail -n 100 -F {shlex.quote(str(self.path))}")
        return False
