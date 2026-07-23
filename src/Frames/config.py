"""Tkinter configuration panel for bot settings and execution."""

from __future__ import annotations


class TextLogs:
    """Simple log textbox wrapper compatible with the original bot API."""

    def __init__(self, textbox=None) -> None:
        self.textbox = textbox

    def addLogs(self, text: str, level: str = "info") -> None:
        if self.textbox is None:
            return
        try:
            self.textbox.insert("end", f"{text}\n")
            self.textbox.see("end")
        except Exception:
            pass

    def set_progress(self, data: dict) -> None:
        return None


class FrameConfig:
    """Compatibility placeholder for the CustomTkinter frame present in the executable."""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def runBot(self) -> None:
        from src.start import StartGame

        StartGame(TextLogs())
