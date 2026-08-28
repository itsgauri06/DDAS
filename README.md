# DDAS — Duplicate Download Alert System

DDAS (Duplicate Download Alert System) is a Windows-based application designed to improve download safety and storage management.

It combines:

- 🔍 Duplicate file detection using SHA-256 hashing
- 🛡️ Suspicious download detection through a Chrome extension
- ⚠️ Real-time security warnings for potentially dangerous downloads
- 🔔 Windows desktop notifications
- 📊 A desktop dashboard for monitoring activity
- 🗑️ Duplicate file management

---

## Features

### 1. Duplicate File Detection

DDAS monitors the user's Downloads folder and identifies duplicate files by calculating their SHA-256 hash.

When two files have identical content, DDAS identifies them as duplicates and displays:

- Duplicate filename
- Original file location
- File size
- Detection time

Users can also:

- Open the original file
- Open the duplicate file
- Delete the duplicate
- Clear duplicate history

---

### 2. Suspicious Download Protection

DDAS includes a Chrome extension that analyzes downloads when they are created.

The extension currently checks for potentially dangerous file types such as:

- `.exe`
- `.msi`
- `.scr`
- `.bat`
- `.cmd`
- `.ps1`
- `.vbs`
- `.js`
- `.dll`
- and other executable/script extensions

It also detects deceptive double extensions such as:

document.pdf.exe
image.jpg.scr

## Installation & Usage

### Requirements

Before installing DDAS, make sure the following are available:

- Windows 10 or Windows 11
- Python 3.x
- Google Chrome
- Internet connection

---

### 1. Clone the Repository

Clone the DDAS repository using Git:

```bash
git clone <repository-url>
cd DDAS
```

---

### 2. Install Python Dependencies

Open a terminal inside the DDAS project folder and install the required Python libraries:

```bash
pip install requests watchdog windows-toasts
```

---

### 3. Load the DDAS Chrome Extension

The Chrome extension is responsible for detecting suspicious downloads.

1. Open Google Chrome.
2. Go to:

```text
chrome://extensions/
```

3. Enable **Developer mode**.
4. Click **Load unpacked**.
5. Select the `extension` folder from the DDAS project.
6. Make sure the **DDAS Download Protection** extension is enabled.

---

### 4. Start the DDAS Communication Bridge

The bridge allows the Chrome extension and the DDAS desktop application to communicate with each other.

Open a terminal in the DDAS project folder and run:

```bash
python bridge.py
```

If successful, the terminal will display:

```text
DDAS Communication Bridge running at http://127.0.0.1:5000
```

**Keep this terminal running while using DDAS.**

---

### 5. Start the DDAS Desktop Application

Open another terminal in the project folder and run:

```bash
python ddas.py
```

The DDAS desktop dashboard will open.
---

## Using DDAS

### Duplicate Detection

When a new file is downloaded:

```text
New File
   ↓
Wait for Download to Finish
   ↓
Calculate SHA-256 Hash
   ↓
Compare With Existing Files
   ↓
Duplicate?
   ├── No  → Add File to Index
   │
   └── Yes → Generate Duplicate Alert
```

If a duplicate is detected, DDAS:

- Displays the duplicate in **Duplicate History**
- Updates the **Duplicates Detected** count
- Calculates potentially reclaimable storage
- Displays a Windows notification
- Records the detection in `ddas_history.json`

The user can then:

- **Open Original** — Opens the original file.
- **Open Duplicate** — Opens the duplicate file.
- **Delete Duplicate** — Deletes the duplicate file.
- **Clear History** — Clears the stored duplicate detection history.

---

### Suspicious Download Protection

The Chrome extension analyzes downloads when they are created.


When a suspicious download is detected:

```text
Download Created
       ↓
Analyze Filename & Extension
       ↓
Suspicious?
   ├── No → Download Normally
   │
   └── Yes
        ↓
      Pause
        ↓
  Security Warning
        ↓
   ┌───────────────┐
   │ Continue      │
   │ Download      │
   │               │
   │ Cancel        │
   │ Download      │
   └───────────────┘
```

### Continue Download

Selecting **Continue Download** allows the paused download to resume.

### Cancel Download

Selecting **Cancel Download** cancels and removes the suspicious download.

The security event is also sent to the DDAS desktop application through the local communication bridge.

---

## Stopping DDAS

To stop the DDAS desktop application, close the dashboard window.

To stop the communication bridge, return to the terminal running `bridge.py` and press:

```text
Ctrl + C
```

The Chrome extension can be disabled or removed from:

```text
chrome://extensions/
```

---

## Important Notes

- The Python bridge must be running for the Chrome extension to communicate with the DDAS dashboard.
- The desktop application monitors the user's `Downloads` folder.
- The Chrome extension must be loaded manually using Chrome's **Load unpacked** option during development.
- Suspicious download detection currently uses filename and extension-based rules and is not a replacement for antivirus software.
- DDAS is currently designed primarily for Windows.
