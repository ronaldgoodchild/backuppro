# BackupPro v3.0 - Installation & Setup Guide

## 📋 System Requirements

### Minimum Requirements
- **OS**: Windows 10 (64-bit) or Windows 11
- **RAM**: 4 GB
- **Disk Space**: 100 MB for the application, plus space for backups/images
- **Python**: 3.11+ (only needed if running `backuppro3.py` directly instead of the compiled `.exe`)

### Recommended Requirements
- **OS**: Windows 10/11 Pro (64-bit)
- **RAM**: 8 GB or more
- **Administrator account**: needed for disk imaging, WinPE media building, and system-level environment backup
- **NAS or network share**: for off-machine backup destinations

### Administrator Privileges
BackupPro will offer to relaunch itself elevated (UAC) on startup. Elevation is required for:
- Disk imaging - both wbAdmin (Volume Shadow Copy) and DISM
- Building WinPE recovery media (uses the Windows ADK's `copype`/`dism`/`MakeWinPEMedia` tools)
- Backing up system-level (`HKLM`) environment variables
- Resetting the Windows Backup Engine service

Most other features (Quick Sync, browser backup, WiFi export, app inventory, user-level environment backup) work fine without elevation.

---

## 🔧 Installation

You can either run the compiled executable (simplest) or run the Python source directly.

### Option A: Run the Compiled Executable

1. Copy `dist\backuppro3.exe` to wherever you want to keep it
2. Double-click to run, or right-click → **Run as administrator** for full functionality
3. The exe keeps its own `backup_config.json` and `backup_log.txt` in whatever folder it's run from - keep it in a stable location so your settings persist between runs

### Option B: Run from Python Source

1. **Install Python 3.11+** from [python.org](https://www.python.org/downloads/) if not already installed
   - Check **"Add Python to PATH"** during installation
2. **Install the one required package:**
   ```bash
   pip install customtkinter
   ```
   Everything else BackupPro uses (`tkinter`, `smtplib`, `winreg`, `urllib`, etc.) is part of the Python standard library - no other dependencies.
3. **Run it:**
   ```bash
   python backuppro3.py
   ```
   For disk imaging or WinPE features, right-click and choose **Run as administrator**, or let the app's own elevation prompt relaunch it for you.

### Rebuilding the Executable (for developers)

The exe is built with PyInstaller using the included spec file:
```bash
pip install pyinstaller
pyinstaller backuppro3.spec --noconfirm
```
This produces `dist\backuppro3.exe`. Rebuild it after any change to `backuppro3.py` - an already-running instance of the old exe keeps running the old code even after the file on disk is replaced, so fully close and relaunch to pick up changes.

---

## ⚙️ Initial Configuration

Open **⚙️ Settings** in the header toolbar to configure everything below. There are no hardcoded defaults for any of this - it ships blank so it works for any user's network, not just one specific setup.

### 1. NAS / Server Monitoring (Optional)

- Enter the IP address of a device you want the dashboard to ping for online/offline status
- Click **🔌 Test** to verify it's reachable
- Leave blank to disable this indicator entirely

### 2. NAS Login (Only Needed for Network Disk Images)

If you'll be imaging to a network share (mapped drive or UNC path), enter the NAS username/password here so you're not prompted every time. BackupPro also prompts for this automatically the first time you image to a network destination, with a "Remember in Settings" option that fills this in for you.

**Why this is needed even if your mapped drive already works:** disk imaging runs elevated (Administrator), and elevated processes run in a separate Windows logon session that can't see the credentials your normal desktop session used to map the drive. BackupPro works around this automatically, but it still needs the actual username/password once.

### 3. Email Notifications (Optional)

1. Enable **"Enable email notifications"**
2. Enter your **Gmail address** as the sender
3. Generate a Gmail **App Password**:
   - Go to your Google Account → Security → 2-Step Verification (must be enabled first)
   - App Passwords → Generate new → name it "BackupPro"
   - Copy the 16-character password
4. Paste it into the **App Password** field (not your regular Gmail password)
5. Click **📤 Send Test Notification** to verify

### 4. SMS Text Alerts (Optional)

Sends texts through your carrier's email-to-SMS gateway using the same Gmail account above - no separate SMS service or paid API needed.

1. Enable **"Enable SMS text alerts"**
2. Click **➕ Add Phone Number**, enter a label, the 10-digit number, and select your carrier
3. Repeat for additional recipients
4. Test with **📤 Send Test Notification**

Carriers occasionally retire their email-to-SMS gateway, particularly prepaid/MVNO carriers - if texts stop arriving after previously working, that's the first thing to check.

### 5. ntfy Push Notifications (Optional)

Free instant push notifications to your phone or desktop - no Gmail account needed for this channel.

1. Enable **"Enable ntfy push notifications"**
2. Click **🎲 Generate** for a random, private topic name (don't use something guessable - anyone who knows the topic name can read your notifications on the public ntfy.sh server)
3. Install the free **ntfy** app (iOS/Android/desktop) and subscribe to the same topic name
4. Test with **📤 Send Test Notification**

### 6. Backup Retention

Set **Keep Last N Backups** (default: 5) to control how many disk images are kept before older ones are automatically deleted.

---

## 🎯 Creating Your First Backup

### Quick Profile Backup (Fastest Start)

1. Launch BackupPro
2. Click **🏠 Quick Profile Backup** in the header
3. Choose a destination
4. BackupPro automatically finds and backs up your Documents, Desktop, Pictures, and other standard profile folders (including OneDrive-redirected ones)

### Quick Sync (Folder Mirroring)

1. Go to the **Quick Sync** tab
2. Browse to a source folder and a destination
3. Optionally enable ZIP compression or dry-run mode
4. Click **RUN SYNC**

### Full System Image

See the manual (`backuppro_manual.html`) sections 7-8 for disk imaging (wbAdmin/VSS or DISM) and building WinPE recovery media - these are covered in depth there rather than duplicated here.

### Save as a Profile

After configuring a backup, save it as a named profile from the **Profiles** tab for one-click reuse later.

---

## 📅 Setting Up Scheduled Backups

1. Go to the **Schedule** tab
2. Select a saved profile
3. Set frequency (daily/weekly/monthly) and time
4. Click to create the scheduled task - BackupPro registers it with Windows Task Scheduler
5. Verify it in Task Scheduler (`taskschd.msc`) under the BackupPro task name

---

## 🥾 WinPE Recovery Media (Bare-Metal Recovery)

For recovering a machine that won't boot at all, BackupPro can build bootable WinPE recovery media bundling your restore scripts and optionally itself.

**Prerequisite:** the **Windows ADK** plus its separate **Windows PE add-on** (both free from Microsoft) must be installed. The **WinPE Media** tab detects this automatically and links to the official download page if missing.

See the manual, section 8, for the full walkthrough.

---

## 🔐 Security Notes

- All credentials (Gmail app password, NAS login, SMS/ntfy settings) are stored in **plain text** in `backup_config.json`. This is a deliberate trade-off for a single-user local tool, not an oversight - protect the config file and any backups of it accordingly.
- Exported WiFi passwords and browser data also contain plain-text sensitive information - store the output securely and delete it when no longer needed.
- Never use your real Gmail password anywhere in BackupPro - always use an App Password, which can be individually revoked without affecting your main account.

---

## 🛠️ Troubleshooting

### Application Won't Start
Run from a command prompt to see the actual error instead of a silent failure:
```bash
python backuppro3.py
```

### "Administrator Required" / Access Denied Errors
Right-click and **Run as administrator**, or accept BackupPro's own elevation prompt on startup.

### NAS Shows "Not Configured" or "Offline"
- "Not Configured" is expected until you enter an IP in Settings - there's no default
- For "Offline": verify the device is powered on, the IP is correct, and it responds to `ping <ip>`

### Backing Up to a Mapped Network Drive Fails
- Let BackupPro convert the destination to its real UNC path when prompted, rather than typing a mapped letter manually
- Enter your NAS credentials when the login dialog appears (check "Remember in Settings" so it only asks once)

### "Another Backup or Recovery Operation Is In Progress"
On the **Disk Image** tab, click **🔄 Reset Backup Service**, wait for it to fully finish, then retry. This restarts the Windows `wbengine` service, which clears a stuck lock left by a previously interrupted backup.

### DISM Capture Fails with "The process cannot access the file"
Click **Yes** when prompted to open `dism.log` - it names the exact locked file. For a fully reliable full-`C:` capture, use the wbAdmin **CREATE SYSTEM IMAGE** button instead (it uses Volume Shadow Copy, which DISM does not), or capture from WinPE recovery media.

### Email / SMS / ntfy Notifications Not Working
Use **📤 Send Test Notification** in Settings - it reports success/failure per channel. Common causes: using a regular Gmail password instead of an App Password, a carrier that's retired its SMS gateway, or an ntfy topic mismatch between Settings and the app you subscribed with.

---

## 📞 Support & Resources

### File Locations
- **Configuration**: `backup_config.json` (same folder as the running exe/script)
- **Log File**: `backup_log.txt`
- **DISM Log** (Windows-level, not BackupPro's own): `C:\Windows\Logs\DISM\dism.log`

### Getting Help
1. Check the in-app Activity Log for the specific error
2. Check `backup_log.txt` for details
3. Contact: the GitHub issue tracker

### Reporting Issues
Include: Windows version, whether you're running the `.exe` or `.py` directly, the relevant lines from `backup_log.txt`, and the exact steps that led to the issue.

---

## ✅ Post-Installation Checklist

- [ ] Application runs without errors (as Administrator, for full functionality)
- [ ] NAS IP configured and tested (if using one)
- [ ] First backup completed successfully
- [ ] Backup profile saved
- [ ] Scheduled task created and verified in Task Scheduler (if using scheduling)
- [ ] Notifications configured and tested via "Send Test Notification" (optional)
- [ ] Windows ADK + WinPE add-on installed (only if you plan to build recovery media)
- [ ] A restore has been tested at least once

---

**BackupPro v3.0** - Ronald Goodchild / REGTeches
