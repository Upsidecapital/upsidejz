"""
GreymatterAI — Settings Dialog
Allows the user to configure the remote server URL and test connectivity.
"""

import requests

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QSizePolicy, QSpacerItem
)

# ---------------------------------------------------------------------------
# Colours (kept in sync with app.py)
# ---------------------------------------------------------------------------
DARK_BG = "#0d1117"
DARK_SURFACE = "#161b22"
DARK_SURFACE2 = "#1c2128"
DARK_BORDER = "#30363d"
DARK_TEXT = "#c9d1d9"
DARK_TEXT_MUTED = "#8b949e"
ACCENT_BLUE = "#58a6ff"
ACCENT_GREEN = "#3fb950"
ACCENT_RED = "#f85149"
ACCENT_YELLOW = "#d29922"

DIALOG_STYLESHEET = f"""
QDialog {{
    background-color: {DARK_BG};
    color: {DARK_TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

QLabel {{
    color: {DARK_TEXT};
    font-size: 13px;
}}

QLabel#sectionTitle {{
    font-size: 18px;
    font-weight: 700;
    color: {DARK_TEXT};
}}

QLabel#subtitle {{
    font-size: 12px;
    color: {DARK_TEXT_MUTED};
}}

QLabel#fieldLabel {{
    font-size: 12px;
    font-weight: 600;
    color: {DARK_TEXT_MUTED};
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

QLineEdit {{
    background-color: {DARK_SURFACE};
    color: {DARK_TEXT};
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 14px;
}}

QLineEdit:focus {{
    border-color: {ACCENT_BLUE};
    background-color: {DARK_SURFACE2};
}}

QPushButton {{
    background-color: {DARK_SURFACE};
    color: {DARK_TEXT};
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
    padding: 7px 16px;
    font-size: 13px;
    min-width: 90px;
}}

QPushButton:hover {{
    background-color: #21262d;
    border-color: {ACCENT_BLUE};
    color: {ACCENT_BLUE};
}}

QPushButton:pressed {{
    background-color: {DARK_BG};
}}

QPushButton#saveBtn {{
    background-color: #238636;
    border-color: #2ea043;
    color: #ffffff;
    font-weight: 600;
}}

QPushButton#saveBtn:hover {{
    background-color: #2ea043;
}}

QPushButton#testBtn {{
    background-color: transparent;
    border: 1px solid {ACCENT_BLUE};
    color: {ACCENT_BLUE};
}}

QPushButton#testBtn:hover {{
    background-color: #1b2d4f;
}}

QPushButton#testBtn:disabled {{
    color: {DARK_TEXT_MUTED};
    border-color: {DARK_BORDER};
    background-color: transparent;
}}

QFrame#divider {{
    background-color: {DARK_BORDER};
    max-height: 1px;
    border: none;
}}
"""


class SettingsDialog(QDialog):
    """
    Modal settings dialog for configuring the GreymatterAI server URL.

    Features:
    - URL input with validation
    - "Test Connection" button that hits GET /health
    - Visual feedback (green tick / red X) without blocking the UI
    - Save / Cancel buttons
    - Dark theme matching the trading dashboard
    """

    def __init__(self, current_url: str = "http://localhost:8000", parent=None):
        super().__init__(parent)
        self._current_url = current_url
        self._saved_url: str = ""
        self._build_ui()
        self.setModal(True)
        self.setFixedSize(520, 370)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle("GreymatterAI \u2014 Settings")
        self.setStyleSheet(DIALOG_STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 24)
        root.setSpacing(0)

        # --- Header ---
        title = QLabel("Settings")
        title.setObjectName("sectionTitle")
        root.addWidget(title)

        subtitle = QLabel("Configure connection to the GreymatterAI trading engine")
        subtitle.setObjectName("subtitle")
        root.addWidget(subtitle)

        root.addSpacing(24)

        # --- Divider ---
        divider_top = QFrame()
        divider_top.setObjectName("divider")
        divider_top.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(divider_top)

        root.addSpacing(20)

        # --- Server URL field ---
        url_label = QLabel("SERVER URL")
        url_label.setObjectName("fieldLabel")
        root.addWidget(url_label)

        root.addSpacing(6)

        self._url_edit = QLineEdit(self._current_url)
        self._url_edit.setPlaceholderText(
            "https://your-railway-url.railway.app"
        )
        self._url_edit.setMinimumHeight(40)
        self._url_edit.textChanged.connect(self._on_url_changed)
        root.addWidget(self._url_edit)

        root.addSpacing(6)

        hint = QLabel(
            "Enter the full URL of your Railway (or VPS) deployment. "
            "Include https:// for secure connections."
        )
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addSpacing(16)

        # --- Test connection row ---
        test_row = QHBoxLayout()
        test_row.setSpacing(10)

        self._test_btn = QPushButton("Test Connection")
        self._test_btn.setObjectName("testBtn")
        self._test_btn.setFixedWidth(150)
        self._test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(self._test_btn)

        self._test_result = QLabel("")
        self._test_result.setMinimumWidth(260)
        self._test_result.setWordWrap(True)
        test_row.addWidget(self._test_result)

        test_row.addStretch()
        root.addLayout(test_row)

        root.addStretch()

        # --- Divider ---
        divider_bottom = QFrame()
        divider_bottom.setObjectName("divider")
        divider_bottom.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(divider_bottom)

        root.addSpacing(16)

        # --- Save / Cancel ---
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._cancel_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("saveBtn")
        self._save_btn.clicked.connect(self._save)
        btn_row.addWidget(self._save_btn)

        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_url_changed(self, text: str):
        """Clear test result when the user edits the URL."""
        self._test_result.setText("")

    def _test_connection(self):
        """
        Perform a synchronous GET /health request and display the result.
        Uses a short timeout so the UI is only blocked for at most ~5 s.
        For a production app with stricter UI requirements, move this to
        a QThread — but for a settings dialog the brief block is acceptable.
        """
        url = self._url_edit.text().strip().rstrip("/")
        if not url:
            self._set_test_result(False, "Please enter a server URL first.")
            return

        if not url.startswith(("http://", "https://")):
            url = "http://" + url

        self._test_btn.setEnabled(False)
        self._test_btn.setText("Testing...")
        self._test_result.setText("")
        # Allow the UI to repaint before we block
        QTimer.singleShot(50, lambda: self._do_test(url))

    def _do_test(self, url: str):
        """Internal: perform the actual HTTP request."""
        try:
            resp = requests.get(f"{url}/health", timeout=8)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    detail = data.get("status") or data.get("message") or "OK"
                except Exception:
                    detail = "OK"
                self._set_test_result(True, f"Connected \u2014 {detail}")
            else:
                self._set_test_result(
                    False,
                    f"Server responded with HTTP {resp.status_code}"
                )
        except requests.exceptions.ConnectionError:
            self._set_test_result(
                False,
                "Connection refused. Is the server running at this URL?"
            )
        except requests.exceptions.Timeout:
            self._set_test_result(False, "Request timed out after 8 seconds.")
        except requests.exceptions.SSLError as exc:
            self._set_test_result(False, f"SSL error: {exc}")
        except Exception as exc:
            self._set_test_result(False, str(exc))
        finally:
            self._test_btn.setEnabled(True)
            self._test_btn.setText("Test Connection")

    def _set_test_result(self, success: bool, message: str):
        """Display a coloured success/failure label."""
        if success:
            icon = "\u2714"  # heavy check mark
            colour = "#3fb950"  # green
        else:
            icon = "\u2718"  # heavy ballot X
            colour = "#f85149"  # red

        self._test_result.setText(f"{icon}  {message}")
        self._test_result.setStyleSheet(f"color: {colour}; font-size: 13px;")

    def _save(self):
        """Validate and save the URL, then close the dialog."""
        url = self._url_edit.text().strip().rstrip("/")
        if not url:
            self._set_test_result(False, "URL cannot be empty.")
            return

        if not url.startswith(("http://", "https://")):
            url = "http://" + url
            self._url_edit.setText(url)

        self._saved_url = url
        self.accept()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_url(self) -> str:
        """Return the URL that was saved (empty string if dialog was cancelled)."""
        return self._saved_url
