from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Button
from textual.containers import Container


HELP_TEXT = """\
[bold bright_cyan]◆[/bold bright_cyan] [bold white]Co[/bold white][bold bright_magenta]Code[/bold bright_magenta] [dim]NL2SQL[/dim]

[bold bright_cyan]Keybindings[/bold bright_cyan]
  [bright_cyan]Enter[/bright_cyan]       Submit query / apply config
  [bright_cyan]Ctrl+N[/bright_cyan]      New session
  [bright_cyan]Ctrl+E[/bright_cyan]      Export results (CSV)
  [bright_cyan]F1[/bright_cyan]          This help
  [bright_cyan]Ctrl+C[/bright_cyan]      Quit

[bold bright_cyan]How it works[/bold bright_cyan]
  1. Type a natural language question
  2. Steps 1–3: Finetuned model generates SQL
  3. Each query executes in a Docker sandbox
  4. An LLM judge evaluates correctness
  5. If rejected, the model retries with feedback
  6. Steps 4–5: A superior model takes over
  7. Best result is displayed

[bold bright_cyan]Configuration[/bold bright_cyan]
  Config:  [dim]nl2sql.json[/dim]
  Secrets: [dim].env (HF_TOKEN)[/dim]
"""


class HelpDialog(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpDialog {
        align: center middle;
    }

    #help-container {
        width: 62;
        height: auto;
        max-height: 32;
        background: #1a1a2e;
        border: tall #4a4a6a;
        padding: 1 2;
    }

    #help-text {
        padding: 1;
    }

    #help-close-btn {
        margin: 1 0 0 0;
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="help-container"):
            yield Static(HELP_TEXT, id="help-text", markup=True)
            yield Button("Close", variant="primary", id="help-close-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key in ("escape", "f1", "q"):
            self.dismiss(None)
