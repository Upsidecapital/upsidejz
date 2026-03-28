# GreymatterAI — Windows Desktop Client

A native Windows desktop application that serves as a **remote control panel** for the GreymatterAI NAS100 trading engine. The actual trading logic runs in the cloud (Railway/VPS); this EXE connects to it over HTTP and embeds the full web dashboard inside a native window.

---

## Quick Start

### Run directly (development)

```
pip install -r requirements.txt
python app.py
```

### Build a standalone EXE

Double-click `build.bat` (Python + pip must be in your system PATH).

The built executable will be at:

```
dist\GreymatterAI\GreymatterAI.exe
```

No Python installation is required on the target machine. Distribute the entire `dist\GreymatterAI\` folder (zip it up).

---

## First-Time Setup

1. Launch `GreymatterAI.exe`.
2. On first run, the **Settings** dialog opens automatically.
3. Enter your Railway deployment URL, e.g.:
   ```
   https://greymatter-production.up.railway.app
   ```
4. Click **Test Connection** to verify the server is reachable.
5. Click **Save**. The dashboard loads immediately.

You can re-open Settings at any time via the **gear icon** in the toolbar or the system tray right-click menu.

---

## Using the Application

| Feature | How to use |
|---|---|
| **Connect to a different server** | Edit the URL in the toolbar and click **Connect** (or press Enter) |
| **Open Settings** | Click the gear icon (⚙) in the toolbar or right-click the tray icon |
| **Minimise to tray** | Click the X button — the app stays running in the system tray |
| **Restore window** | Double-click the tray icon, or right-click → **Show Dashboard** |
| **Quit completely** | Right-click the tray icon → **Quit GreymatterAI** |

### Status chip (top-right of toolbar)

| Colour | Meaning |
|---|---|
| Green — ACTIVE | Trading engine is running and monitoring the market |
| Red — HALTED | Engine is connected but trading has been paused |
| Grey — DISCONNECTED | Cannot reach the server at the configured URL |
| Yellow — CONNECTING | Initial connection or reconnecting |

The status is refreshed every **15 seconds** by polling `/api/stats`. A Windows toast notification appears whenever the status changes (e.g. ACTIVE → HALTED).

---

## Configuration

Settings are stored in the **Windows registry** (via Qt's `QSettings`):

```
HKEY_CURRENT_USER\Software\GreymatterAI\GreymatterAI
```

Key: `greymatter/server_url`

You can edit this with `regedit` if needed, though the Settings dialog is easier.

---

## Build Requirements

- Python 3.11 or 3.12 (64-bit)
- `pip install pyinstaller PyQt6==6.7.1 PyQt6-WebEngine==6.7.0 requests==2.32.3`

### Custom icon

Replace `assets/icon.ico` with your own 256×256 `.ico` file before running `build.bat`. The spec file (`greymatter.spec`) references `assets/icon.ico`.

### Code-signing (optional, recommended)

After building, sign the EXE to avoid Windows SmartScreen warnings:

```batch
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
    /f your-cert.pfx /p your-password ^
    dist\GreymatterAI\GreymatterAI.exe
```

---

## Architecture

```
GreymatterAI.exe  (this app)
    │
    │  HTTP polls /api/stats every 15 s
    │  QWebEngineView embeds full dashboard
    │
    └──► Railway / VPS
             greymatter_ai/  (trading engine)
             ├── Dockerfile
             ├── main.py
             └── ...
```

- **No trading logic** runs locally. This EXE is purely a display and control layer.
- The web dashboard is rendered by Chromium (bundled with PyQt6-WebEngine) — it looks and behaves identically to opening the URL in Chrome.
- HTTP status polling happens on a background `QThread` so the UI never freezes.

---

## Deploying the Trading Engine

See `greymatter_ai/Dockerfile` in the repository root. Deploy to Railway:

1. Push your code to GitHub.
2. Create a new Railway project, connect the repo.
3. Railway will build and deploy automatically.
4. Copy the generated Railway URL (e.g. `https://xxx.up.railway.app`) into this app's Settings.

---

## Troubleshooting

**"Cannot connect to server" / DISCONNECTED status**
- Verify the server URL in Settings is correct and includes the protocol (`https://`).
- Check that the Railway deployment is running (Railway dashboard → Deployments).
- Ensure there is no firewall blocking outbound HTTPS from your machine.

**Dashboard shows a blank white page**
- The server may still be starting up. Wait 30 seconds and click Connect again.
- Check `/health` endpoint manually in your browser.

**Build fails with "No module named PyQt6.QtWebEngineWidgets"**
- Run: `pip install PyQt6-WebEngine==6.7.0`
- Make sure you are using a 64-bit Python installation.

**EXE triggers Windows SmartScreen**
- This is expected for unsigned EXEs. Click "More info" → "Run anyway".
- For production distribution, sign the EXE (see Code-signing section above).

**Tray icon not visible**
- Windows may hide the tray icon. Click the ^ arrow in the taskbar notification area to find it and drag it to the always-visible area.
