"""
GreymatterAI — Windows Desktop Client
Remote control panel for the cloud-deployed trading engine.
No Python script to run — just double-click GreymatterAI.exe

This application is a thin remote control panel. The actual trading engine
runs in the cloud (Railway/VPS). This EXE connects to it via HTTP and embeds
the full web dashboard inside a native window.

Architecture:
  - QMainWindow with QWebEngineView for the embedded dashboard
  - QSystemTrayIcon for minimize-to-tray behaviour
  - QTimer polling /api/stats every 15 seconds for status updates
  - QThread worker for non-blocking HTTP requests
  - QSettings (Windows registry) for persistent configuration
"""

import sys
import json
import datetime

from PyQt6.QtCore import (
    Qt, QUrl, QTimer, QSettings, QThread, pyqtSignal, QObject, QSize
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QColor, QPainter, QFont, QAction, QPen
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLineEdit, QPushButton, QLabel, QStatusBar, QSystemTrayIcon,
    QMenu, QToolBar, QFrame, QSizePolicy, QMessageBox
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_NAME = "GreymatterAI"
ORG_NAME = "GreymatterAI"
WINDOW_TITLE = "GreymatterAI \u2014 NAS100 Prop Desk"
DEFAULT_SERVER_URL = "http://localhost:8000"
POLL_INTERVAL_MS = 15_000  # 15 seconds

# Status values
STATUS_ACTIVE = "ACTIVE"
STATUS_HALTED = "HALTED"
STATUS_DISCONNECTED = "DISCONNECTED"
STATUS_CONNECTING = "CONNECTING"

# Dark theme colours matching the trading dashboard
DARK_BG = "#0d1117"
DARK_SURFACE = "#161b22"
DARK_BORDER = "#30363d"
DARK_TEXT = "#c9d1d9"
DARK_TEXT_MUTED = "#8b949e"
ACCENT_BLUE = "#58a6ff"
ACCENT_GREEN = "#3fb950"
ACCENT_RED = "#f85149"
ACCENT_YELLOW = "#d29922"

# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------
DARK_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {DARK_BG};
    color: {DARK_TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

QToolBar {{
    background-color: {DARK_SURFACE};
    border-bottom: 1px solid {DARK_BORDER};
    padding: 4px 8px;
    spacing: 6px;
}}

QLineEdit {{
    background-color: {DARK_BG};
    color: {DARK_TEXT};
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 13px;
}}

QLineEdit:focus {{
    border-color: {ACCENT_BLUE};
}}

QPushButton {{
    background-color: {DARK_SURFACE};
    color: {DARK_TEXT};
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
    padding: 5px 14px;
    font-size: 13px;
    min-width: 80px;
}}

QPushButton:hover {{
    background-color: #21262d;
    border-color: {ACCENT_BLUE};
}}

QPushButton:pressed {{
    background-color: #161b22;
}}

QPushButton#connectBtn {{
    background-color: #238636;
    border-color: #2ea043;
    color: #ffffff;
    font-weight: 600;
}}

QPushButton#connectBtn:hover {{
    background-color: #2ea043;
}}

QPushButton#settingsBtn {{
    background-color: transparent;
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
    font-size: 16px;
    min-width: 36px;
    max-width: 36px;
    padding: 3px;
}}

QPushButton#settingsBtn:hover {{
    background-color: #21262d;
}}

QStatusBar {{
    background-color: {DARK_SURFACE};
    color: {DARK_TEXT_MUTED};
    border-top: 1px solid {DARK_BORDER};
    font-size: 12px;
    padding: 2px 8px;
}}

QMenu {{
    background-color: {DARK_SURFACE};
    color: {DARK_TEXT};
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 20px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: #21262d;
    color: {ACCENT_BLUE};
}}

QMenu::separator {{
    height: 1px;
    background: {DARK_BORDER};
    margin: 4px 8px;
}}

QLabel#statusChip {{
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.5px;
    padding: 3px 10px;
    border-radius: 10px;
}}
"""


# ---------------------------------------------------------------------------
# HTTP Worker — runs requests in a background QThread so the UI never freezes
# ---------------------------------------------------------------------------
class StatsWorker(QObject):
    """Polls /api/stats and emits results on the Qt signal bus."""

    result = pyqtSignal(dict)   # emitted on success: parsed JSON dict
    error = pyqtSignal(str)     # emitted on failure: error message string

    def __init__(self, server_url: str):
        super().__init__()
        self.server_url = server_url.rstrip("/")

    def fetch(self):
        """Perform the GET request. Called from a QThread."""
        try:
            url = f"{self.server_url}/api/stats"
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            self.result.emit(data)
        except requests.exceptions.ConnectionError:
            self.error.emit("Cannot connect to server")
        except requests.exceptions.Timeout:
            self.error.emit("Request timed out")
        except requests.exceptions.HTTPError as exc:
            self.error.emit(f"HTTP {exc.response.status_code}")
        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Status chip widget
# ---------------------------------------------------------------------------
class StatusChip(QLabel):
    """Coloured pill label showing ACTIVE / HALTED / DISCONNECTED."""

    _COLOURS = {
        STATUS_ACTIVE:       (ACCENT_GREEN,  "#0d2315"),
        STATUS_HALTED:       (ACCENT_RED,    "#2d0f0e"),
        STATUS_DISCONNECTED: (DARK_TEXT_MUTED, "#1c1f24"),
        STATUS_CONNECTING:   (ACCENT_YELLOW,  "#2d2209"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statusChip")
        self._current = STATUS_DISCONNECTED
        self.set_status(STATUS_DISCONNECTED)

    def set_status(self, status: str) -> str:
        """Update chip text/colour. Returns previous status."""
        previous = self._current
        self._current = status
        fg, bg = self._COLOURS.get(status, (DARK_TEXT_MUTED, "#1c1f24"))
        self.setText(status)
        self.setStyleSheet(
            f"QLabel#statusChip {{"
            f"  color: {fg};"
            f"  background-color: {bg};"
            f"  border: 1px solid {fg};"
            f"  font-size: 12px; font-weight: 700;"
            f"  letter-spacing: 0.5px;"
            f"  padding: 3px 10px;"
            f"  border-radius: 10px;"
            f"}}"
        )
        return previous

    @property
    def current(self) -> str:
        return self._current


# ---------------------------------------------------------------------------
# Tray icon helper — generates a simple coloured square icon at runtime
# so no external .ico file is required during development
# ---------------------------------------------------------------------------
def _make_tray_icon(colour: str = ACCENT_BLUE) -> QIcon:
    """Generate a 32x32 icon programmatically."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 28, 28, 6, 6)
    # Draw a small "G" letter
    painter.setPen(QPen(QColor("#ffffff"), 1))
    font = QFont("Segoe UI", 14, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "G")
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class GreymatterWindow(QMainWindow):
    """
    Main application window.

    Layout:
      ┌──────────────────────────────────────────────────────┐
      │  [URL Field ──────────────────] [Connect] [⚙] Status│  ← toolbar
      ├──────────────────────────────────────────────────────┤
      │                                                      │
      │               QWebEngineView                         │
      │         (embeds the trading dashboard)               │
      │                                                      │
      ├──────────────────────────────────────────────────────┤
      │  Last updated: 14:23:01  ·  GreymatterAI v1.0        │  ← status bar
      └──────────────────────────────────────────────────────┘
    """

    def __init__(self):
        super().__init__()
        self._settings = QSettings(ORG_NAME, APP_NAME)
        self._server_url: str = self._settings.value(
            "greymatter/server_url", DEFAULT_SERVER_URL
        )
        self._poll_thread: QThread | None = None
        self._worker: StatsWorker | None = None
        self._first_run: bool = not bool(
            self._settings.value("greymatter/server_url")
        )

        self._build_ui()
        self._build_tray()
        self._start_poll_timer()

        # First-run: prompt for server URL
        if self._first_run:
            QTimer.singleShot(500, self._open_settings)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1400, 900)
        self.setStyleSheet(DARK_STYLESHEET)

        # Try to use a real icon if assets/icon.ico exists
        icon_path = "assets/icon.ico"
        import os
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        else:
            self.setWindowIcon(_make_tray_icon())

        # ---- Toolbar ----
        toolbar = QToolBar("Main Toolbar", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(toolbar)

        # Branding label
        brand = QLabel("  GreymatterAI  ")
        brand.setStyleSheet(
            f"color: {ACCENT_BLUE}; font-weight: 700; font-size: 15px; "
            f"letter-spacing: 1px; padding-right: 8px;"
        )
        toolbar.addWidget(brand)

        # Divider
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet(f"color: {DARK_BORDER};")
        divider.setFixedHeight(24)
        toolbar.addWidget(divider)

        # Spacer
        spacer_left = QWidget()
        spacer_left.setFixedWidth(8)
        toolbar.addWidget(spacer_left)

        # URL input
        self._url_edit = QLineEdit(self._server_url)
        self._url_edit.setPlaceholderText("https://your-railway-url.railway.app")
        self._url_edit.setMinimumWidth(340)
        self._url_edit.setMaximumWidth(520)
        self._url_edit.returnPressed.connect(self._on_connect)
        toolbar.addWidget(self._url_edit)

        spacer_mid = QWidget()
        spacer_mid.setFixedWidth(6)
        toolbar.addWidget(spacer_mid)

        # Connect button
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setObjectName("connectBtn")
        self._connect_btn.setFixedWidth(90)
        self._connect_btn.clicked.connect(self._on_connect)
        toolbar.addWidget(self._connect_btn)

        spacer_mid2 = QWidget()
        spacer_mid2.setFixedWidth(6)
        toolbar.addWidget(spacer_mid2)

        # Settings gear button
        self._settings_btn = QPushButton("\u2699")
        self._settings_btn.setObjectName("settingsBtn")
        self._settings_btn.setToolTip("Settings")
        self._settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(self._settings_btn)

        # Flexible spacer to push status chip to the right
        flex_spacer = QWidget()
        flex_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        toolbar.addWidget(flex_spacer)

        # Status chip
        self._status_chip = StatusChip()
        toolbar.addWidget(self._status_chip)

        spacer_right = QWidget()
        spacer_right.setFixedWidth(8)
        toolbar.addWidget(spacer_right)

        # ---- Web engine view ----
        self._web_view = QWebEngineView()

        # Enable useful web features for the dashboard
        page_settings = self._web_view.page().settings()
        page_settings.setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptEnabled, True
        )
        page_settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalStorageEnabled, True
        )
        page_settings.setAttribute(
            QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True
        )
        page_settings.setAttribute(
            QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True
        )

        self._web_view.loadStarted.connect(self._on_load_started)
        self._web_view.loadFinished.connect(self._on_load_finished)

        self.setCentralWidget(self._web_view)

        # ---- Status bar ----
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Connecting...")
        self._status_bar.addWidget(self._status_label)

        # Load initial URL
        self._load_dashboard()

    def _build_tray(self):
        """Set up the system tray icon and its context menu."""
        import os
        icon_path = "assets/icon.ico"
        if os.path.isfile(icon_path):
            tray_icon = QIcon(icon_path)
        else:
            tray_icon = _make_tray_icon()

        self._tray = QSystemTrayIcon(tray_icon, self)
        self._tray.setToolTip("GreymatterAI Trading")

        tray_menu = QMenu()

        show_action = QAction("Show Dashboard", self)
        show_action.triggered.connect(self._show_window)
        tray_menu.addAction(show_action)

        tray_menu.addSeparator()

        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self._open_settings)
        tray_menu.addAction(settings_action)

        tray_menu.addSeparator()

        quit_action = QAction("Quit GreymatterAI", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    # ------------------------------------------------------------------
    # Navigation / loading
    # ------------------------------------------------------------------

    def _load_dashboard(self):
        """Navigate the web view to the configured server URL."""
        url = self._server_url.rstrip("/") + "/"
        self._web_view.setUrl(QUrl(url))
        self._status_chip.set_status(STATUS_CONNECTING)

    def _on_load_started(self):
        self._status_chip.set_status(STATUS_CONNECTING)
        self._status_label.setText("Loading dashboard...")

    def _on_load_finished(self, ok: bool):
        if ok:
            self._status_label.setText(
                f"Loaded \u2022 {datetime.datetime.now().strftime('%H:%M:%S')}"
            )
        else:
            self._status_chip.set_status(STATUS_DISCONNECTED)
            self._status_label.setText(
                "Failed to load dashboard \u2014 check server URL in Settings"
            )

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _start_poll_timer(self):
        """Kick off the 15-second QTimer that polls /api/stats."""
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_stats)
        self._poll_timer.start()
        # Also poll immediately after a short delay
        QTimer.singleShot(2000, self._poll_stats)

    def _poll_stats(self):
        """
        Spawn a QThread to fetch /api/stats without blocking the UI.
        If the previous thread is still running, skip this cycle.
        """
        if self._poll_thread and self._poll_thread.isRunning():
            return

        self._poll_thread = QThread()
        self._worker = StatsWorker(self._server_url)
        self._worker.moveToThread(self._poll_thread)

        # Wire up signals
        self._poll_thread.started.connect(self._worker.fetch)
        self._worker.result.connect(self._on_stats_result)
        self._worker.error.connect(self._on_stats_error)
        self._worker.result.connect(self._poll_thread.quit)
        self._worker.error.connect(self._poll_thread.quit)
        self._poll_thread.finished.connect(self._poll_thread.deleteLater)

        self._poll_thread.start()

    def _on_stats_result(self, data: dict):
        """Handle a successful /api/stats response."""
        # Try common status field names from the greymatter backend
        raw_status = (
            data.get("status")
            or data.get("system_status")
            or data.get("trading_status")
            or ""
        ).upper()

        if raw_status in (STATUS_ACTIVE, "RUNNING", "LIVE"):
            new_status = STATUS_ACTIVE
        elif raw_status in (STATUS_HALTED, "STOPPED", "PAUSED", "IDLE"):
            new_status = STATUS_HALTED
        else:
            new_status = STATUS_ACTIVE  # connected and got data — treat as active

        previous = self._status_chip.set_status(new_status)

        # Show notification only when status actually changes
        if previous != new_status and previous not in (
            STATUS_CONNECTING, STATUS_DISCONNECTED
        ):
            self._notify_status_change(previous, new_status)

        now = datetime.datetime.now().strftime("%H:%M:%S")
        self._status_label.setText(f"Last updated: {now}")

    def _on_stats_error(self, message: str):
        """Handle a failed /api/stats request."""
        previous = self._status_chip.set_status(STATUS_DISCONNECTED)
        if previous not in (STATUS_DISCONNECTED, STATUS_CONNECTING):
            self._tray.showMessage(
                "GreymatterAI — Connection Lost",
                f"Cannot reach trading engine: {message}",
                QSystemTrayIcon.MessageIcon.Critical,
                4000,
            )
        self._status_label.setText(
            f"Disconnected \u2014 {message} \u2022 "
            f"{datetime.datetime.now().strftime('%H:%M:%S')}"
        )

    def _notify_status_change(self, old: str, new: str):
        """Fire a Windows toast notification on status transition."""
        if new == STATUS_ACTIVE:
            icon = QSystemTrayIcon.MessageIcon.Information
            title = "GreymatterAI \u2014 Trading ACTIVE"
            body = "The trading engine is now active and monitoring markets."
        elif new == STATUS_HALTED:
            icon = QSystemTrayIcon.MessageIcon.Warning
            title = "GreymatterAI \u2014 Trading HALTED"
            body = "The trading engine has been halted. No new trades will be placed."
        else:
            icon = QSystemTrayIcon.MessageIcon.Critical
            title = "GreymatterAI \u2014 Disconnected"
            body = "Lost connection to the trading engine."

        self._tray.showMessage(title, body, icon, 5000)

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------

    def _on_connect(self):
        """Apply the URL from the toolbar input and reload."""
        url = self._url_edit.text().strip().rstrip("/")
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
            self._url_edit.setText(url)

        self._server_url = url
        self._settings.setValue("greymatter/server_url", url)
        self._load_dashboard()
        # Restart poll timer so it immediately checks the new server
        self._poll_timer.stop()
        self._poll_timer.start()
        QTimer.singleShot(1500, self._poll_stats)

    def _open_settings(self):
        """Open the Settings dialog."""
        # Import here to avoid circular imports at module level
        from settings_dialog import SettingsDialog

        dialog = SettingsDialog(self._server_url, parent=self)
        if dialog.exec():
            new_url = dialog.get_url()
            if new_url and new_url != self._server_url:
                self._server_url = new_url
                self._settings.setValue("greymatter/server_url", new_url)
                self._url_edit.setText(new_url)
                self._load_dashboard()

    # ------------------------------------------------------------------
    # Tray and window management
    # ------------------------------------------------------------------

    def _show_window(self):
        """Restore the window from minimised/hidden state."""
        self.show()
        self.setWindowState(
            self.windowState() & ~Qt.WindowState.WindowMinimized
            | Qt.WindowState.WindowActive
        )
        self.raise_()
        self.activateWindow()

    def _quit_app(self):
        """Clean shutdown — stop timers and exit."""
        self._poll_timer.stop()
        if self._poll_thread and self._poll_thread.isRunning():
            self._poll_thread.quit()
            self._poll_thread.wait(2000)
        self._tray.hide()
        QApplication.quit()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """Double-click tray icon to show/toggle the window."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            if self.isVisible():
                self.hide()
            else:
                self._show_window()

    def closeEvent(self, event):
        """
        Override close (X button) to minimise to tray instead of quitting.
        The user must use Tray → Quit to fully exit.
        """
        event.ignore()
        self.hide()
        self._tray.showMessage(
            "GreymatterAI",
            "Minimised to system tray. Right-click the tray icon to quit.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationDisplayName(WINDOW_TITLE)

    # Prevent the app from quitting when the last window is hidden
    # (we still live in the system tray)
    app.setQuitOnLastWindowClosed(False)

    window = GreymatterWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
