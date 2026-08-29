#QQ-Music-third-party-Python-project

> When the QQ Music third-party API meets DeepSeek — control your music with natural language.

A command-line (and Tkinter GUI) AI music player for **QQ Music**. Describe what you want to hear in plain language — *"Play Jay Chou's Sunny Day"* — and the DeepSeek AI decides which API call to make: search, play, pause, skip, download, recommend, and more.

**⚠️ Unofficial project.** This uses the third-party `qqmusic-api-python` library and is intended for personal / educational use only. Respect copyright and the QQ Music Terms of Service.

---

## ✨ Features

- 🗣️ **Natural-language control** — powered by DeepSeek (function calling)
- 🔍 **Search & play** — search a song and start playback in one step
- ⏯️ **Playback control** — play / pause / resume / stop / next / previous
- 🎚️ **Live progress bar** — terminal progress bar + automatic track switching
- ⬇️ **High-quality download** — auto-fallback from `MASTER (FLAC)` → `ATMOS (FLAC)` → `FLAC` → `MP3 320k` → `MP3 128k`
- 📱 **Phone + SMS login** — with session reuse via `credential.json`
- 🖥️ **Optional Tkinter GUI** — dark-themed music player window
- 🎧 **mpv playback backend** — with graceful fallback if mpv is missing

---

## 🏗️ Architecture

```
┌─────────────┐   natural language   ┌──────────────┐   function call   ┌──────────────┐
│    User     │ ───────────────────▶ │ DeepSeekAgent │ ────────────────▶ │  QQMusic API │
│ (CLI / GUI) │ ◀─────────────────── │  (DeepSeek)   │ ◀──────────────── │ qqmusic-api- │
└─────────────┘        reply         └──────────────┘      result        │    python    │
                                            │  play / pause / skip          └──────┬───────┘
                                            ▼                                     │ search / url
                                     ┌─────────────┐                            │ download
                                     │  MPVPlayer  │ ◀──────────────────────────┘
                                     │    (mpv)    │
                                     └─────────────┘
```

| Module | Responsibility |
|--------|----------------|
| `QQMusicClient` | Wrap `qqmusic-api-python`: login (phone+SMS), search, play URL, download, playlists, recommendations |
| `MPVPlayer` | Wrap mpv: play / pause / resume / stop / volume / position / duration (subprocess + JSON IPC) |
| `DeepSeekAgent` | Talk to DeepSeek, map user intent to function calls via tools |
| `MusicPlayerGUI` | Optional Tkinter window |

---

## 📋 Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | **3.10+** (tested on 3.14) | Download from [python.org](https://www.python.org/downloads/) |
| mpv | any recent | [mpv.io](https://mpv.io/installation/) or `winget install --id=shinchiro.mpv -e` |
| DeepSeek API key | — | Get one from [platform.deepseek.com](https://platform.deepseek.com/) |

> **Python 3.14 note:** make sure to tick **"Add python.exe to PATH"** during installation.

---

## 🚀 Installation

```bash
# 1. Clone the repo
git clone https://github.com/zhuguang-studio/QQ-Music-third-party-Python-project.git
cd QQ-Music-third-party-Python-project

# 2. Install dependencies
pip install -r requirements_qqmusic.txt
```

Dependencies (`requirements_qqmusic.txt`):

```
openai>=1.0.0          # DeepSeek (OpenAI-compatible API)
qqmusic-api-python     # third-party QQ Music API
python-mpv             # optional mpv bindings (falls back to subprocess)
httpx                  # download support
tqdm                   # download progress bar
pywin32                # Windows named-pipe IPC (Windows only)
```

---

## ⚙️ Configuration

### 1. Set your DeepSeek API key

**Windows (PowerShell):**

```powershell
$env:DEEPSEEK_API_KEY = "sk-xxxxxxxxxxxxxxxx"
```

**Linux / macOS:**

```bash
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx"
```

### 2. Set your Python path (Windows users)

Edit `start.bat` and change the hardcoded path to **your own Python executable**:

```bat
@echo off
set DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
C:\Users\YOUR_NAME\AppData\Local\Programs\Python\Python314\python.exe qq_music_ai_player.py 2>nul
```

> Find your Python path with: `where python`

---

## ▶️ How to run

**Option A — one-click (Windows):**

Double-click `start.bat`, or in a terminal:

```powershell
./start.bat
```

**Option B — direct:**

```bash
python qq_music_ai_player.py
```

### First launch — login

The program logs in with **phone + SMS verification code**:

```
[登录] 请输入手机号: 138xxxx1234
[登录] 正在向 138xxxx1234 发送验证码...
[登录] ⚠️ 需要完成图形验证码，正在用浏览器打开...
（complete the captcha in the browser, then press Enter）
[登录] 📩 验证码已发送，请注意查收短信。
[登录] 请输入短信验证码: 654321
[登录] 🎉 登录成功！
```

Credentials are saved to `credential.json` and reused on the next launch.

---

## 💬 Natural-language commands

| You say (Chinese) | Action |
|-------------------|--------|
| `播放周杰伦的晴天` | search + play |
| `下载稻香` | search + download (best quality) |
| `下一首` / `切歌` / `next` | play next track |
| `暂停` / `pause` | pause |
| `继续` / `继续播放` | resume |
| `停止` / `stop` | stop |
| `推荐一些轻音乐` | get recommendations & play |

Built-in shortcuts: `next` / `下一首` / `pause` / `暂停` / `resume` / `继续` / `stop` / `停止` are matched **locally** (no AI call, zero latency).

---

## ❓ Troubleshooting

### 1. `缺少依赖库: qqmusic-api-python`
The library isn't installed.

```bash
pip install qqmusic-api-python
```

### 2. `[错误] 未设置 DEEPSEEK_API_KEY 环境变量`
You forgot to set the environment variable. See [Configuration](#-configuration).
> On Windows, each new terminal needs `$env:DEEPSEEK_API_KEY = "..."`, or put it in `start.bat`.

### 3. `[播放器] ⚠️ 未找到 mpv，无法播放音乐`
mpv isn't installed or not on `PATH`.

```powershell
winget install --id=shinchiro.mpv -e
```

If installed but not found, the program also searches common locations (`C:\Program Files\MPV Player\mpv.exe`, scoop, etc.). If yours is elsewhere, add it to `_find_mpv()`.

### 4. `发送验证码失败` / `errMsg: 'no phoneNo'`
The API requires the phone number to be passed as an **integer**, not a string (a string is treated as an *encrypted* number). The program already handles this — make sure you're using the latest code.

### 5. Login triggers a **CAPTCHA**
QQ Music's risk-control may require a slider captcha before sending the SMS. The program opens the captcha URL in your browser — complete it, then press **Enter** in the terminal to retry.

### 6. `无法获取播放链接 (result=104003)`
The play-URL API returns an empty `purl` **unless you pass the credential explicitly**. The program passes `credential=self._api.credential` — if you see this, your session may have expired. Delete `credential.json` and log in again.

### 7. `[IPC] 命令发送失败` or progress bar stuck on `加载中...`
`win32file.ReadFile` returns `(error_code, data)` — the **second** element is the actual bytes. The program reads it correctly now; if you still see this, ensure `pywin32` is installed:

```bash
pip install pywin32
```

### 8. `Calling Tcl from different apartment` (Windows)
Tkinter **cannot** run in a background thread on Windows. The program runs `mainloop()` on the main thread and the asyncio loop in a background thread — keep this structure if you modify the GUI.

### 9. `DeprecationWarning: asyncio.WindowsSelectorEventLoopPolicy` (Python 3.14+)
Harmless. The program filters this warning; it's caused by an obsolete Windows event-loop policy call.

### 10. AI replies with text but doesn't execute (e.g. "下一首" does nothing)
Known behavior of chat models. The program mitigates it three ways:
1. Local keyword matching for simple commands (no AI involved)
2. `temperature=0.3` to reduce randomness
3. Force-retry with `tool_choice="required"` when the AI returns empty text

### 11. Songs auto-skip immediately after starting
The auto-advance only triggers when playback reaches **97%** for 2 consecutive seconds. If it still skips early, your song's `duration` may be misreported by mpv — try a different track.

---

## 🔒 Security

- **Do NOT commit `credential.json`** — it contains your QQ Music login credentials.
- **Do NOT commit your DeepSeek API key.** Prefer environment variables over hardcoding.
- A `.gitignore` is included to exclude these files.

```gitignore
# credentials & secrets
credential.json
start.bat

# runtime
downloads/
qrcode.png
__pycache__/
*.pyc
```

> ⚠️ If you already committed `credential.json` or an API key, **revoke the key / log out immediately** — GitHub history keeps the secret.

---

## 📄 Disclaimer

This project is **not affiliated with Tencent / QQ Music**. It relies on a third-party, unofficial API library (`qqmusic-api-python`), which may break at any time. Use it only for personal, non-commercial, educational purposes and at your own risk.

---

## 📜 License

[MIT](LICENSE)
