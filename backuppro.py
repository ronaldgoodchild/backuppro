#!/usr/bin/env python3
"""
BackupPro v3.0 - Professional Edition
Complete backup suite with security fixes, error handling, and full functionality.

Author: Updated & Secured Version
Date: 2026-02-06
"""

import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog, messagebox, simpledialog
import os
import sys
import shutil
import json
import datetime
import threading
import subprocess
import ctypes
import smtplib
import hashlib
import queue
import re
import logging
import webbrowser
import urllib.request
import urllib.parse
import secrets
from pathlib import Path
try:
    import keyring  # secrets go to Windows Credential Manager, not the JSON file
except ImportError:
    keyring = None
from email.message import EmailMessage
from typing import Optional, Dict, List, Any

# --- Configuration ---
CONFIG_FILE = "backup_config.json"
KEYRING_SERVICE = "BackupPro"
SECRET_SETTINGS = ("nas_password", "email_password")  # kept in the credential vault
LOG_FILE = "backup_log.txt"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB
DEFAULT_SETTINGS = {
    "keep_limit": 5,
    "nas_ip": "",
    "nas_username": "",
    "nas_password": "",
    "sender_email": "",
    "email_password": "",
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "notification_enabled": False,
    "sms_enabled": False,
    "sms_recipients": [],  # list of {"phone": "5551234567", "carrier": "Verizon", "label": "Ron"}
    "ntfy_enabled": False,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
}
WINPE_ADK_DOWNLOAD_URL = "https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install"

# Email-to-SMS gateways - texts are sent as plain email to phone@gateway via the Gmail
# account configured in Settings, no separate SMS service or API key needed.
SMS_CARRIERS = {
    "AT&T":              "txt.att.net",
    "T-Mobile":          "tmomail.net",
    "Verizon":           "vtext.com",
    "Sprint":            "messaging.sprintpcs.com",
    "Xfinity / Comcast": "vtext.com",
    "US Cellular":       "email.uscc.net",
    "Boost Mobile":      "sms.myboostmobile.com",
    "Cricket":           "sms.cricketwireless.net",
    "Metro PCS":         "mymetropcs.com",
    "Google Fi":         "msg.fi.google.com",
    "Mint Mobile":       "tmomail.net",
    "Visible":           "vtext.com",
    "Consumer Cellular": "mailmymobile.net",
    "Straight Talk":     "vtext.com",
}

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def is_admin() -> bool:
    """Check if running with administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception as e:
        logger.error(f"Failed to check admin status: {e}")
        return False


def find_winpe_adk() -> Optional[Dict[str, str]]:
    """Locate the Windows ADK's WinPE deployment tools (copype.cmd, MakeWinPEMedia.cmd, DandISetEnv.bat)."""
    kits_roots = []

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots") as key:
            kits_root, _ = winreg.QueryValueEx(key, "KitsRoot10")
            kits_roots.append(kits_root)
    except Exception:
        pass

    kits_roots.extend([
        r"C:\Program Files (x86)\Windows Kits\10",
        r"C:\Program Files\Windows Kits\10",
    ])

    for kits_root in kits_roots:
        adk_root = os.path.join(kits_root, "Assessment and Deployment Kit")
        winpe_root = os.path.join(adk_root, "Windows Preinstallation Environment")
        deploy_root = os.path.join(adk_root, "Deployment Tools")

        copype = os.path.join(winpe_root, "copype.cmd")
        makewinpemedia = os.path.join(winpe_root, "MakeWinPEMedia.cmd")
        setenv = os.path.join(deploy_root, "DandISetEnv.bat")

        if os.path.exists(copype) and os.path.exists(makewinpemedia):
            return {
                "winpe_root": winpe_root,
                "copype": copype,
                "makewinpemedia": makewinpemedia,
                "setenv": setenv if os.path.exists(setenv) else None,
            }

    return None


def validate_path(path: str) -> bool:
    """Validate that a path is safe and exists."""
    if not path or not isinstance(path, str):
        return False
    
    # Check for dangerous characters
    dangerous_chars = ['|', '&', ';', '<', '>', '`', '$', '\n', '\r']
    if any(char in path for char in dangerous_chars):
        return False
    
    try:
        # Normalize path and check if it exists
        normalized = os.path.normpath(path)
        return os.path.exists(normalized)
    except Exception:
        return False


def sanitize_path(path: str) -> str:
    """Sanitize a file path for safe usage."""
    return os.path.normpath(path).strip()


class BackupEngine:
    """Core backup operations engine - separated from UI."""
    
    def __init__(self, ui_callback=None):
        self.ui_callback = ui_callback
        self.is_running = False
        self.current_operation = None
        self.should_cancel = False
        
    def _update_ui(self, message: str, progress: float = None, level: str = "INFO"):
        """Send updates to UI via callback."""
        if self.ui_callback:
            self.ui_callback(message, progress, level)
    
    def cancel_operation(self):
        """Request cancellation of current operation."""
        self.should_cancel = True
        self._update_ui("Cancellation requested...", level="WARNING")
    
    def sync_folders(self, source: str, destination: str, dry_run: bool = False) -> bool:
        """
        Synchronize folders using robocopy.
        
        Args:
            source: Source directory path
            destination: Destination directory path
            dry_run: If True, only simulate the operation
            
        Returns:
            True if successful, False otherwise
        """
        if not validate_path(source):
            self._update_ui(f"Invalid source path: {source}", level="ERROR")
            return False
        
        if not os.path.exists(destination):
            try:
                os.makedirs(destination, exist_ok=True)
            except Exception as e:
                self._update_ui(f"Cannot create destination: {e}", level="ERROR")
                return False
        
        self.is_running = True
        self.should_cancel = False
        
        try:
            source = sanitize_path(source)
            destination = sanitize_path(destination)
            
            # Build robocopy command as list (safer than shell=True)
            cmd = [
                'robocopy',
                source,
                destination,
                '/MIR',  # Mirror mode
                '/Z',    # Restartable mode
                '/R:2',  # Retry 2 times
                '/W:5',  # Wait 5 seconds between retries
                '/MT:16', # Multi-threaded
                '/NP',   # No progress display (we'll parse output)
                '/NDL',  # No directory list
                '/NFL'   # No file list (for cleaner output)
            ]
            
            if dry_run:
                cmd.append('/L')  # List only mode
            
            self._update_ui(f"Starting sync: {source} → {destination}")
            self._update_ui("This may take several minutes...", progress=0.1)
            
            # Run robocopy
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            
            # Monitor for cancellation
            while process.poll() is None:
                if self.should_cancel:
                    process.terminate()
                    self._update_ui("Operation cancelled by user", level="WARNING")
                    return False
                threading.Event().wait(0.5)
            
            returncode = process.returncode
            
            # Robocopy return codes: 0-7 are success, 8+ are errors
            if returncode < 8:
                self._update_ui("Sync completed successfully!", progress=1.0, level="SUCCESS")
                logger.info(f"Sync successful: {source} → {destination}")
                return True
            else:
                stderr = process.stderr.read() if process.stderr else "Unknown error"
                self._update_ui(f"Sync failed with code {returncode}: {stderr}", level="ERROR")
                logger.error(f"Sync failed: {stderr}")
                return False
                
        except Exception as e:
            self._update_ui(f"Sync error: {str(e)}", level="ERROR")
            logger.exception("Sync operation failed")
            return False
        finally:
            self.is_running = False
            self.should_cancel = False
    
    def connect_nas_share(self, unc_path: str, username: str, password: str):
        """Establish an authenticated SMB session to a NAS share for this process.

        Needed because elevated/Administrator processes often can't see the normal
        user's mapped-drive credentials (separate logon session token under UAC) -
        this explicitly authenticates the current (elevated) process to the share.
        Returns (True, "") on success, (False, error_detail) on failure.
        """
        parts = unc_path.strip('\\/').split('\\')
        if len(parts) < 2:
            return False, f"Not a valid UNC path: {unc_path}"
        share_root = f"\\\\{parts[0]}\\{parts[1]}"

        # Drop any existing (possibly unauthenticated) session to this share first
        subprocess.run(
            ['net', 'use', share_root, '/delete', '/y'],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )

        result = subprocess.run(
            ['net', 'use', share_root, password, f'/user:{username}'],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        if result.returncode == 0:
            return True, ""
        detail = result.stdout.strip() or result.stderr.strip() or f"exit code {result.returncode}"
        return False, detail

    def create_disk_image(self, drive: str, destination: str, keep_count: int = 5,
                           nas_username: str = "", nas_password: str = "") -> bool:
        """
        Create a system disk image using wbAdmin.

        Args:
            drive: Drive letter to image (e.g., "C:")
            destination: Destination path for backup
            keep_count: Number of backups to keep
            nas_username: Optional NAS login for network (UNC) destinations
            nas_password: Optional NAS password for network (UNC) destinations

        Returns:
            True if successful, False otherwise
        """
        if not is_admin():
            self._update_ui("Administrator privileges required for disk imaging!", level="ERROR")
            return False

        is_network_target = destination.startswith('\\\\')

        if is_network_target and nas_username:
            self._update_ui(f"Connecting to NAS as {nas_username}...")
            connected, detail = self.connect_nas_share(destination, nas_username, nas_password)
            if not connected:
                self._update_ui(f"NAS login failed: {detail}", level="ERROR")
                return False

        if not validate_path(destination):
            self._update_ui(f"Invalid destination path: {destination}", level="ERROR")
            return False

        self.is_running = True
        self.should_cancel = False

        try:
            destination = sanitize_path(destination)

            # Create timestamped backup folder
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"SystemImage_{drive.replace(':', '')}_{timestamp}"
            backup_path = os.path.join(destination, backup_name)

            self._update_ui(f"Creating system image of {drive}...")
            self._update_ui("This operation may take 30+ minutes...", progress=0.1)

            # Build wbadmin command
            cmd = [
                'wbadmin',
                'start',
                'backup',
                f'-backupTarget:{destination}',
                f'-include:{drive}',
                '-allCritical',
                '-quiet'
            ]
            if is_network_target and nas_username:
                cmd.extend([f'-user:{nas_username}', f'-password:{nas_password}'])

            # Run wbadmin
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            # Continuously drain both pipes on background threads while we poll for
            # cancellation - wbadmin (like dism) writes its error text to stdout, not
            # stderr, and without draining, enough output can fill the OS pipe buffer
            # and block wbadmin mid-write, hanging the whole operation.
            output_chunks = []

            def drain(stream, parse_progress=False):
                if not stream:
                    return
                for line in iter(stream.readline, ''):
                    output_chunks.append(line)
                    if parse_progress:
                        # wbadmin prints periodic "NN% Completed" style lines to stdout -
                        # surface these as real progress instead of just the generic bounce.
                        match = re.search(r'(\d{1,3})\s*%', line)
                        if match:
                            pct = min(int(match.group(1)), 100) / 100
                            self._update_ui(line.strip(), progress=pct)
                stream.close()

            stdout_thread = threading.Thread(target=drain, args=(process.stdout, True), daemon=True)
            stderr_thread = threading.Thread(target=drain, args=(process.stderr, False), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            # Monitor process
            while process.poll() is None:
                if self.should_cancel:
                    process.terminate()
                    self._update_ui("Operation cancelled by user", level="WARNING")
                    return False
                threading.Event().wait(1)

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

            if process.returncode == 0:
                self._update_ui("System image created successfully!", progress=1.0, level="SUCCESS")
                logger.info(f"System image created: {drive} → {destination}")

                # Cleanup old backups
                self._cleanup_old_backups(destination, keep_count)
                return True
            else:
                detail = "".join(output_chunks).strip()
                detail = detail[-800:] if detail else f"exit code {process.returncode}"
                self._update_ui(f"Imaging failed: {detail}", level="ERROR")
                logger.error(f"Imaging failed: {detail}")
                return False
                
        except Exception as e:
            self._update_ui(f"Imaging error: {str(e)}", level="ERROR")
            logger.exception("Disk imaging failed")
            return False
        finally:
            self.is_running = False
            self.should_cancel = False
    
    def _cleanup_old_backups(self, backup_dir: str, keep_count: int):
        """Remove old backups keeping only the most recent ones."""
        try:
            backups = []
            for item in os.listdir(backup_dir):
                item_path = os.path.join(backup_dir, item)
                if os.path.isdir(item_path) and item.startswith("SystemImage_"):
                    backups.append((item_path, os.path.getctime(item_path)))
            
            # Sort by creation time (oldest first)
            backups.sort(key=lambda x: x[1])
            
            # Remove oldest backups if we exceed keep_count
            if len(backups) > keep_count:
                for backup_path, _ in backups[:-keep_count]:
                    shutil.rmtree(backup_path)
                    self._update_ui(f"Removed old backup: {os.path.basename(backup_path)}")
                    logger.info(f"Cleaned up old backup: {backup_path}")
        except Exception as e:
            logger.warning(f"Failed to cleanup old backups: {e}")
    
    def check_nas_status(self, nas_ip: str) -> bool:
        """Check if NAS is reachable."""
        try:
            # Use ping with timeout
            if sys.platform == 'win32':
                cmd = ['ping', '-n', '1', '-w', '1000', nas_ip]
            else:
                cmd = ['ping', '-c', '1', '-W', '1', nas_ip]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=3
            )
            
            return result.returncode == 0
        except Exception as e:
            logger.error(f"NAS check failed: {e}")
            return False


class BackupPro(ctk.CTk):
    """Main application window with complete UI implementation."""
    
    def __init__(self):
        super().__init__()
        
        self.title("BackupPro v3.0 | Professional Backup Suite")
        self.geometry("1300x900")
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        
        # Initialize components
        self._migrate_secrets = False
        self.config = self.load_config()
        if self._migrate_secrets:
            self.save_config()  # moves legacy plain-text passwords into the credential vault
            logger.info("Migrated plain-text passwords to Windows Credential Manager")
        self.engine = BackupEngine(ui_callback=self.handle_engine_update)
        self.ui_queue = queue.Queue()
        self.current_operation_thread = None
        self.operation_in_progress = False
        
        # Setup UI
        self.setup_ui()
        self.refresh_profiles_list()
        self.refresh_schedule_list()
        self.update_nas_status()
        
        # Start UI update loop
        self.after(100, self.process_ui_queue)
        
        logger.info("BackupPro v3.0 started successfully")
    
    # ==================== CONFIGURATION MANAGEMENT ====================
    
    def load_config(self) -> Dict[str, Any]:
        """Load configuration from file with validation."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                # Validate and merge with defaults
                if not isinstance(config, dict):
                    raise ValueError("Invalid config structure")
                
                # Ensure required keys exist
                if "settings" not in config:
                    config["settings"] = {}
                file_settings = config["settings"]
                config["settings"] = {**DEFAULT_SETTINGS, **file_settings}
                # Secrets live in Windows Credential Manager; a plain-text value in the
                # file is a legacy config and is moved into the vault on the next save.
                if keyring:
                    for key in SECRET_SETTINGS:
                        if not file_settings.get(key):
                            try:
                                config["settings"][key] = keyring.get_password(KEYRING_SERVICE, key) or ""
                            except Exception as e:
                                logger.error(f"Credential vault read failed for {key}: {e}")
                    if any(file_settings.get(k) for k in SECRET_SETTINGS):
                        self._migrate_secrets = True
                
                if "profiles" not in config:
                    config["profiles"] = []
                
                logger.info("Configuration loaded successfully")
                return config
                
            except Exception as e:
                logger.error(f"Failed to load config: {e}")
                messagebox.showwarning("Config Error", f"Failed to load config: {e}\nUsing defaults.")
        
        # Return default config
        return {"profiles": [], "settings": DEFAULT_SETTINGS.copy()}
    
    def save_config(self):
        """Save configuration to file."""
        try:
            to_write = {**self.config, "settings": dict(self.config.get("settings", {}))}
            if keyring:
                for key in SECRET_SETTINGS:
                    value = to_write["settings"].get(key, "")
                    try:
                        if value:
                            keyring.set_password(KEYRING_SERVICE, key, value)
                        else:
                            try:
                                keyring.delete_password(KEYRING_SERVICE, key)
                            except Exception:
                                pass  # nothing stored
                        to_write["settings"][key] = ""
                    except Exception as e:
                        logger.error(f"Credential vault unavailable for {key}, saving in plain text: {e}")
            else:
                if any(to_write["settings"].get(k) for k in SECRET_SETTINGS):
                    logger.warning("keyring not installed - passwords saved in plain text (pip install keyring)")
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(to_write, f, indent=4)
            logger.info("Configuration saved successfully")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            messagebox.showerror("Save Error", f"Failed to save configuration: {e}")
    
    # ==================== UI SETUP ====================
    
    def setup_ui(self):
        """Build the complete user interface."""
        
        # Header
        header = ctk.CTkFrame(self, height=80, corner_radius=0)
        header.pack(fill="x", padx=0, pady=0)
        header.pack_propagate(False)
        
        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.pack(side="left", padx=20, fill="y")
        
        ctk.CTkLabel(
            title_frame,
            text="💾 BackupPro v3.0",
            font=("Segoe UI", 24, "bold")
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            title_frame,
            text="Professional Backup Suite",
            font=("Segoe UI", 12),
            text_color="gray70"
        ).pack(anchor="w")
        
        # Header buttons
        button_frame = ctk.CTkFrame(header, fg_color="transparent")
        button_frame.pack(side="right", padx=20)
        
        self.cancel_btn = ctk.CTkButton(
            button_frame,
            text="⏹ Cancel",
            width=100,
            fg_color="#E74C3C",
            hover_color="#C0392B",
            command=self.cancel_operation,
            state="disabled"
        )
        self.cancel_btn.pack(side="right", padx=5)
        
        ctk.CTkButton(
            button_frame,
            text="❌ Exit",
            width=100,
            fg_color="#E74C3C",
            hover_color="#C0392B",
            command=self.on_closing
        ).pack(side="right", padx=5)
        
        ctk.CTkButton(
            button_frame,
            text="🏠 Quick Profile Backup",
            width=160,
            fg_color="#27AE60",
            hover_color="#229954",
            command=self.quick_user_backup
        ).pack(side="right", padx=5)
        
        ctk.CTkButton(
            button_frame,
            text="🔄 NAS Check",
            width=100,
            command=self.update_nas_status
        ).pack(side="right", padx=5)
        
        ctk.CTkButton(
            button_frame,
            text="⚙️ Settings",
            width=100,
            command=self.show_settings
        ).pack(side="right", padx=5)
        
        ctk.CTkButton(
            button_frame,
            text="❓ Help",
            width=100,
            command=self.show_help
        ).pack(side="right", padx=5)
        
        # Main container
        main_container = ctk.CTkFrame(self, fg_color="transparent")
        main_container.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Left side - Tabs
        self.tabs = ctk.CTkTabview(main_container, width=750)
        self.tabs.pack(side="left", fill="both", expand=True, padx=(0, 5))
        
        self.tabs.add("Quick Sync")
        self.tabs.add("Disk Image")
        self.tabs.add("Tools")
        self.tabs.add("WinPE Media")
        self.tabs.add("Restore")
        self.tabs.add("Profiles")
        self.tabs.add("Schedule")

        self.build_sync_tab()
        self.build_imaging_tab()
        self.build_tools_tab()
        self.build_winpe_tab()
        self.build_restore_tab()
        self.build_profiles_tab()
        self.build_schedule_tab()
        
        # Right side - Status panel
        right_panel = ctk.CTkFrame(main_container, width=500)
        right_panel.pack(side="right", fill="both", expand=False)
        right_panel.pack_propagate(False)
        
        # Status header
        status_header = ctk.CTkFrame(right_panel, height=60, corner_radius=0)
        status_header.pack(fill="x", padx=0, pady=0)
        status_header.pack_propagate(False)
        
        ctk.CTkLabel(
            status_header,
            text="📊 Activity Monitor",
            font=("Arial", 16, "bold")
        ).pack(side="left", padx=15, pady=10)
        
        self.nas_status_label = ctk.CTkLabel(
            status_header,
            text="NAS: Checking...",
            font=("Arial", 11),
            text_color="gray"
        )
        self.nas_status_label.pack(side="right", padx=15)
        
        # Progress section
        progress_frame = ctk.CTkFrame(right_panel, fg_color="transparent")
        progress_frame.pack(fill="x", padx=15, pady=10)
        
        self.status_label = ctk.CTkLabel(
            progress_frame,
            text="Ready",
            font=("Arial", 12, "bold")
        )
        self.status_label.pack(anchor="w", pady=(0, 5))
        
        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.pack(fill="x")
        self.progress_bar.set(0)
        
        # Log display
        log_container = ctk.CTkFrame(right_panel)
        log_container.pack(fill="both", expand=True, padx=15, pady=10)
        
        ctk.CTkLabel(
            log_container,
            text="Event Log",
            font=("Arial", 12, "bold")
        ).pack(anchor="w", padx=5, pady=5)
        
        self.log_text = ctk.CTkTextbox(
            log_container,
            font=("Consolas", 10),
            wrap="word"
        )
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_text.configure(state="disabled")
        
        # Clear log button
        ctk.CTkButton(
            log_container,
            text="Clear Log",
            width=100,
            height=28,
            command=self.clear_log
        ).pack(pady=5)
    
    def build_sync_tab(self):
        """Build the Quick Sync tab."""
        tab = self.tabs.tab("Quick Sync")
        
        # Header
        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkLabel(
            header,
            text="📁 Folder Synchronization",
            font=("Arial", 16, "bold")
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            header,
            text="Mirror source folder to destination (⚠️ Destination will match source exactly)",
            font=("Arial", 11),
            text_color="gray70"
        ).pack(anchor="w", pady=(5, 0))
        
        # Source path
        self.sync_src = self.create_path_selector(
            tab,
            "📂 Source Folder:",
            "Select the folder to backup"
        )
        
        # Destination path
        self.sync_dst = self.create_path_selector(
            tab,
            "💾 Destination Folder:",
            "Select where to store the backup"
        )
        
        # Options
        options_frame = ctk.CTkFrame(tab, fg_color="transparent")
        options_frame.pack(fill="x", padx=40, pady=10)
        
        self.sync_dryrun = ctk.CTkCheckBox(
            options_frame,
            text="Dry Run (preview only, no changes)",
            font=("Arial", 11)
        )
        self.sync_dryrun.pack(anchor="w", pady=5)
        
        self.sync_compress = ctk.CTkCheckBox(
            options_frame,
            text="📦 Create ZIP archive (saves 50-70% space)",
            font=("Arial", 11)
        )
        self.sync_compress.pack(anchor="w", pady=5)
        
        # Action buttons
        button_frame = ctk.CTkFrame(tab, fg_color="transparent")
        button_frame.pack(fill="x", padx=40, pady=20)
        
        ctk.CTkButton(
            button_frame,
            text="▶️ RUN SYNC",
            font=("Arial", 14, "bold"),
            fg_color="#27AE60",
            hover_color="#229954",
            height=50,
            command=self.run_sync
        ).pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(
            button_frame,
            text="💾 Save as Profile",
            command=self.save_sync_profile
        ).pack(fill="x")
    
    def build_imaging_tab(self):
        """Build the Disk Image tab."""
        tab = self.tabs.tab("Disk Image")
        
        # Header
        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkLabel(
            header,
            text="💿 System Disk Imaging",
            font=("Arial", 16, "bold")
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            header,
            text="Create complete system images using Windows Backup (requires Admin)",
            font=("Arial", 11),
            text_color="gray70"
        ).pack(anchor="w", pady=(5, 0))
        
        # Admin warning
        if not is_admin():
            warning = ctk.CTkFrame(tab, fg_color="#E74C3C")
            warning.pack(fill="x", padx=40, pady=10)
            ctk.CTkLabel(
                warning,
                text="⚠️ Administrator privileges required for disk imaging",
                font=("Arial", 11, "bold"),
                text_color="white"
            ).pack(padx=10, pady=10)
        
        # Drive selection
        drive_frame = ctk.CTkFrame(tab, fg_color="transparent")
        drive_frame.pack(fill="x", padx=40, pady=10)
        
        ctk.CTkLabel(
            drive_frame,
            text="💽 Drive to Image:",
            font=("Arial", 12, "bold")
        ).pack(anchor="w", pady=(0, 5))
        
        # Get available drives
        drives = self.get_available_drives()
        self.img_drive = ctk.CTkComboBox(
            drive_frame,
            values=drives if drives else ["C:", "D:"],
            width=200
        )
        self.img_drive.pack(anchor="w")
        
        # Destination
        self.img_dest = self.create_path_selector(
            tab,
            "💾 Backup Destination:",
            "Select where to store system images"
        )
        
        # Retention settings
        retention_frame = ctk.CTkFrame(tab, fg_color="transparent")
        retention_frame.pack(fill="x", padx=40, pady=10)
        
        ctk.CTkLabel(
            retention_frame,
            text="🗄️ Keep Last:",
            font=("Arial", 12, "bold")
        ).pack(anchor="w", pady=(0, 5))
        
        self.img_keep = ctk.CTkSegmentedButton(
            retention_frame,
            values=["3", "5", "10", "15"]
        )
        self.img_keep.set("5")
        self.img_keep.pack(anchor="w")
        
        ctk.CTkLabel(
            retention_frame,
            text="Older backups will be automatically deleted",
            font=("Arial", 10),
            text_color="gray70"
        ).pack(anchor="w", pady=(5, 0))
        
        # Action button
        ctk.CTkButton(
            tab,
            text="🔷 CREATE SYSTEM IMAGE",
            font=("Arial", 14, "bold"),
            fg_color="#E67E22",
            hover_color="#D35400",
            height=50,
            command=self.run_image
        ).pack(fill="x", padx=40, pady=20)

        ctk.CTkButton(
            tab,
            text="🔄 Stuck on \"another backup is in progress\"? Reset Backup Service",
            font=("Arial", 10),
            fg_color="transparent",
            text_color="#3498DB",
            hover_color="#1E2B38",
            height=28,
            command=self.reset_backup_service
        ).pack(fill="x", padx=40, pady=(0, 10))

        # DISM alternative method
        dism_frame = ctk.CTkFrame(tab)
        dism_frame.pack(fill="x", padx=20, pady=(0, 20))

        ctk.CTkLabel(
            dism_frame,
            text="🧱 DISM System Image (Alternative Method)",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            dism_frame,
            text="A file-based .wim image captured/applied with DISM. For a full OS drive, best captured "
                 "while booted into WinPE (see the WinPE Media tab) rather than live from Windows.",
            font=("Arial", 10),
            text_color="gray60",
            wraplength=650,
            justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 10))

        dism_btn_frame = ctk.CTkFrame(dism_frame, fg_color="transparent")
        dism_btn_frame.pack(fill="x", padx=20, pady=(0, 15))

        ctk.CTkButton(
            dism_btn_frame,
            text="📀 Capture Image (DISM)",
            font=("Arial", 12, "bold"),
            fg_color="#2C3E50",
            hover_color="#1B2631",
            height=40,
            command=self.capture_dism_image
        ).pack(fill="x", pady=(0, 8))

        ctk.CTkButton(
            dism_btn_frame,
            text="📝 Generate DISM Restore Script",
            font=("Arial", 12, "bold"),
            fg_color="#2C3E50",
            hover_color="#1B2631",
            height=40,
            command=self.generate_dism_restore_script
        ).pack(fill="x")

    def build_profiles_tab(self):
        """Build the Profiles management tab."""
        tab = self.tabs.tab("Profiles")
        
        # Header
        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkLabel(
            header,
            text="📋 Backup Profiles",
            font=("Arial", 16, "bold")
        ).pack(side="left")
        
        ctk.CTkButton(
            header,
            text="➕ New Profile",
            width=120,
            command=self.create_new_profile
        ).pack(side="right")
        
        # Profiles list
        self.profiles_scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.profiles_scroll.pack(fill="both", expand=True, padx=20, pady=10)
    
    def build_schedule_tab(self):
        """Build the Schedule tab."""
        tab = self.tabs.tab("Schedule")
        
        # Header
        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkLabel(
            header,
            text="⏰ Scheduled Tasks",
            font=("Arial", 16, "bold")
        ).pack(side="left")
        
        ctk.CTkButton(
            header,
            text="➕ New Schedule",
            width=120,
            command=self.create_schedule
        ).pack(side="right")
        
        # Info
        info_frame = ctk.CTkFrame(tab, fg_color="#3498DB")
        info_frame.pack(fill="x", padx=40, pady=10)
        
        ctk.CTkLabel(
            info_frame,
            text="ℹ️ Automated tasks run via Windows Task Scheduler",
            font=("Arial", 11),
            text_color="white"
        ).pack(padx=15, pady=10)
        
        # Scheduled tasks list
        self.schedule_scroll = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.schedule_scroll.pack(fill="both", expand=True, padx=20, pady=10)
    
    def build_tools_tab(self):
        """Build the Tools tab with browser backup and WiFi export."""
        tab = self.tabs.tab("Tools")
        
        # Header
        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkLabel(
            header,
            text="🛠️ Backup Tools",
            font=("Arial", 16, "bold")
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            header,
            text="Additional backup utilities for browser data, WiFi passwords, and system migration",
            font=("Arial", 11),
            text_color="gray70"
        ).pack(anchor="w", pady=(5, 0))
        
        # Browser Backup Section
        browser_frame = ctk.CTkFrame(tab)
        browser_frame.pack(fill="x", padx=20, pady=10)
        
        ctk.CTkLabel(
            browser_frame,
            text="🌐 Browser Data Backup",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))
        
        ctk.CTkLabel(
            browser_frame,
            text="Export bookmarks, passwords, and profiles from Chrome, Firefox, and Edge",
            font=("Arial", 10),
            text_color="gray60"
        ).pack(anchor="w", padx=20, pady=(0, 10))
        
        browser_btn_frame = ctk.CTkFrame(browser_frame, fg_color="transparent")
        browser_btn_frame.pack(fill="x", padx=20, pady=(0, 15))
        
        ctk.CTkButton(
            browser_btn_frame,
            text="🌐 Backup All Browsers",
            font=("Arial", 13, "bold"),
            fg_color="#3498DB",
            hover_color="#2980B9",
            height=45,
            command=self.backup_browsers
        ).pack(fill="x")
        
        # WiFi Export Section
        wifi_frame = ctk.CTkFrame(tab)
        wifi_frame.pack(fill="x", padx=20, pady=10)
        
        ctk.CTkLabel(
            wifi_frame,
            text="📶 WiFi Password Export",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))
        
        ctk.CTkLabel(
            wifi_frame,
            text="Export all saved WiFi networks and passwords to a text file",
            font=("Arial", 10),
            text_color="gray60"
        ).pack(anchor="w", padx=20, pady=(0, 10))
        
        wifi_btn_frame = ctk.CTkFrame(wifi_frame, fg_color="transparent")
        wifi_btn_frame.pack(fill="x", padx=20, pady=(0, 15))
        
        ctk.CTkButton(
            wifi_btn_frame,
            text="📶 Export WiFi Passwords",
            font=("Arial", 13, "bold"),
            fg_color="#9B59B6",
            hover_color="#8E44AD",
            height=45,
            command=self.export_wifi_passwords
        ).pack(fill="x")

        # App Inventory / Migration Section
        apps_frame = ctk.CTkFrame(tab)
        apps_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            apps_frame,
            text="📦 Installed Apps Inventory",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            apps_frame,
            text="Export your installed apps (via winget) plus a one-click script to reinstall them all on a clean PC",
            font=("Arial", 10),
            text_color="gray60"
        ).pack(anchor="w", padx=20, pady=(0, 10))

        apps_btn_frame = ctk.CTkFrame(apps_frame, fg_color="transparent")
        apps_btn_frame.pack(fill="x", padx=20, pady=(0, 15))

        ctk.CTkButton(
            apps_btn_frame,
            text="📦 Export App List + Restore Script",
            font=("Arial", 13, "bold"),
            fg_color="#E67E22",
            hover_color="#D35400",
            height=45,
            command=self.export_app_inventory
        ).pack(fill="x")

        # Environment & Registry Backup Section
        env_frame = ctk.CTkFrame(tab)
        env_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            env_frame,
            text="🖥️ Environment Variables & Registry",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            env_frame,
            text="Backup user/system environment variables (.reg files) plus a restore script to re-import them",
            font=("Arial", 10),
            text_color="gray60"
        ).pack(anchor="w", padx=20, pady=(0, 10))

        env_btn_frame = ctk.CTkFrame(env_frame, fg_color="transparent")
        env_btn_frame.pack(fill="x", padx=20, pady=(0, 15))

        ctk.CTkButton(
            env_btn_frame,
            text="🖥️ Backup Environment Variables",
            font=("Arial", 13, "bold"),
            fg_color="#16A085",
            hover_color="#138D75",
            height=45,
            command=self.backup_environment_vars
        ).pack(fill="x")

        # Info box
        info_frame = ctk.CTkFrame(tab, fg_color="#27AE60")
        info_frame.pack(fill="x", padx=40, pady=20)

        ctk.CTkLabel(
            info_frame,
            text="💡 TIP: Combine App Inventory + Environment Backup for a full new-PC migration kit — run the generated scripts after a clean Windows install to get everything back the way you like it.",
            font=("Arial", 11),
            text_color="white",
            wraplength=650,
            justify="left"
        ).pack(padx=15, pady=10)

    def build_winpe_tab(self):
        """Build the WinPE Recovery Media tab."""
        tab = self.tabs.tab("WinPE Media")
        self.winpe_bundle_folders = []

        # Header
        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=15)

        ctk.CTkLabel(
            header,
            text="🥾 WinPE Recovery Media",
            font=("Arial", 16, "bold")
        ).pack(anchor="w")

        ctk.CTkLabel(
            header,
            text="Build a bootable WinPE ISO/USB that bundles your restore scripts and can apply DISM images",
            font=("Arial", 11),
            text_color="gray70",
            wraplength=650,
            justify="left"
        ).pack(anchor="w", pady=(5, 0))

        # ADK status
        adk = find_winpe_adk()
        status_color = "#27AE60" if adk else "#E74C3C"
        status_text = (f"✅ Windows ADK WinPE tools found:\n{adk['winpe_root']}" if adk
                       else "❌ Windows ADK (with the WinPE add-on) was not found on this system")
        status_frame = ctk.CTkFrame(tab, fg_color=status_color)
        status_frame.pack(fill="x", padx=40, pady=10)
        ctk.CTkLabel(
            status_frame,
            text=status_text,
            font=("Arial", 11, "bold"),
            text_color="white",
            wraplength=650,
            justify="left"
        ).pack(padx=10, pady=(10, 2 if not adk else 10))

        if not adk:
            ctk.CTkLabel(
                status_frame,
                text="Install the 'Windows ADK' and the 'Windows PE add-on' from Microsoft (both required), then reopen this tab.",
                font=("Arial", 10),
                text_color="white",
                wraplength=650,
                justify="left"
            ).pack(padx=10, pady=(0, 5))

            ctk.CTkButton(
                status_frame,
                text="🔗 Open Windows ADK Download Page",
                font=("Arial", 11, "bold"),
                fg_color="#2C3E50",
                hover_color="#1B2631",
                height=35,
                command=self.open_adk_download_page
            ).pack(padx=10, pady=(0, 10), fill="x")

        if not is_admin():
            warning = ctk.CTkFrame(tab, fg_color="#E74C3C")
            warning.pack(fill="x", padx=40, pady=(0, 10))
            ctk.CTkLabel(
                warning,
                text="⚠️ Administrator privileges required to build WinPE media",
                font=("Arial", 11, "bold"),
                text_color="white"
            ).pack(padx=10, pady=10)

        # Bundle content
        bundle_frame = ctk.CTkFrame(tab)
        bundle_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            bundle_frame,
            text="📁 Restore Content to Include (optional)",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            bundle_frame,
            text="Add your App Inventory / Environment Backup / DISM restore-script folders from the "
                 "Tools and Disk Image tabs — they'll be copied to X:\\REGTeches\\ inside the boot environment",
            font=("Arial", 10),
            text_color="gray60",
            wraplength=650,
            justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 10))

        self.winpe_folder_list_frame = ctk.CTkFrame(bundle_frame, fg_color="transparent")
        self.winpe_folder_list_frame.pack(fill="x", padx=20)

        ctk.CTkLabel(
            self.winpe_folder_list_frame,
            text="No folders added yet",
            font=("Arial", 10),
            text_color="gray60"
        ).pack(anchor="w", pady=5)

        ctk.CTkButton(
            bundle_frame,
            text="➕ Add Folder to Include",
            fg_color="#3498DB",
            hover_color="#2980B9",
            height=35,
            command=self.add_winpe_bundle_folder
        ).pack(fill="x", padx=20, pady=(5, 15))

        # BackupPro Application
        app_frame = ctk.CTkFrame(tab)
        app_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            app_frame,
            text="🗄️ BackupPro Application",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))

        ctk.CTkLabel(
            app_frame,
            text="Include the compiled backuppro3.exe so you can run backups/restores directly from WinPE, "
                 "not just the generated scripts",
            font=("Arial", 10),
            text_color="gray60",
            wraplength=650,
            justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 10))

        self.winpe_include_app = ctk.CTkCheckBox(app_frame, text="Include BackupPro on this WinPE media")
        self.winpe_include_app.pack(anchor="w", padx=20, pady=(0, 10))

        app_path_row = ctk.CTkFrame(app_frame, fg_color="transparent")
        app_path_row.pack(fill="x", padx=20)

        self.winpe_app_path_entry = ctk.CTkEntry(app_path_row, placeholder_text="Path to backuppro3.exe", height=35)
        self.winpe_app_path_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        ctk.CTkButton(
            app_path_row,
            text="📁 Browse",
            width=100,
            command=self.browse_winpe_app_exe
        ).pack(side="right")

        self.winpe_app_status_label = ctk.CTkLabel(
            app_frame,
            text="",
            font=("Arial", 9),
            text_color="gray60",
            wraplength=650,
            justify="left"
        )
        self.winpe_app_status_label.pack(anchor="w", padx=20, pady=(5, 15))

        self.autodetect_winpe_app_exe()

        # Build options
        options_frame = ctk.CTkFrame(tab)
        options_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(
            options_frame,
            text="⚙️ Build Options",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=20, pady=(15, 5))

        arch_row = ctk.CTkFrame(options_frame, fg_color="transparent")
        arch_row.pack(fill="x", padx=20, pady=5)
        ctk.CTkLabel(arch_row, text="Architecture:", font=("Arial", 11)).pack(side="left", padx=(0, 10))
        self.winpe_arch = ctk.CTkSegmentedButton(arch_row, values=["amd64", "x86", "arm64"])
        self.winpe_arch.set("amd64")
        self.winpe_arch.pack(side="left")

        self.winpe_add_powershell = ctk.CTkCheckBox(
            options_frame,
            text="Add PowerShell + DISM cmdlets support (needed to run .ps1 restore scripts)"
        )
        self.winpe_add_powershell.select()
        self.winpe_add_powershell.pack(anchor="w", padx=20, pady=(10, 5))

        self.winpe_cleanup = ctk.CTkCheckBox(
            options_frame,
            text="Delete temporary build workspace when finished"
        )
        self.winpe_cleanup.select()
        self.winpe_cleanup.pack(anchor="w", padx=20, pady=(0, 15))

        # Output target
        target_row = ctk.CTkFrame(options_frame, fg_color="transparent")
        target_row.pack(fill="x", padx=20, pady=(0, 15))
        ctk.CTkLabel(target_row, text="Output:", font=("Arial", 11)).pack(side="left", padx=(0, 10))
        self.winpe_output_type = ctk.CTkSegmentedButton(target_row, values=["ISO File", "USB Drive"])
        self.winpe_output_type.set("ISO File")
        self.winpe_output_type.pack(side="left")

        ctk.CTkLabel(
            options_frame,
            text="USB Drive erases the target drive completely — you'll be asked to confirm the exact drive letter.",
            font=("Arial", 9),
            text_color="gray60",
            wraplength=650,
            justify="left"
        ).pack(anchor="w", padx=20, pady=(0, 15))

        # Build button — every build asks where to save the ISO / which USB drive to use, every time
        ctk.CTkButton(
            tab,
            text="🥾 BUILD WINPE RECOVERY MEDIA",
            font=("Arial", 14, "bold"),
            fg_color="#8E44AD",
            hover_color="#6C3483",
            height=50,
            command=self.build_winpe_media
        ).pack(fill="x", padx=40, pady=(20, 5))

        ctk.CTkButton(
            tab,
            text="🔄 Build failed with a WIM \"still mounted\" error? Clean Up Stuck Mount",
            font=("Arial", 10),
            fg_color="transparent",
            text_color="#3498DB",
            hover_color="#1E2B38",
            height=28,
            command=self.cleanup_stuck_winpe_mount
        ).pack(fill="x", padx=40, pady=(0, 20))

    def cleanup_stuck_winpe_mount(self):
        """Clean up an orphaned WIM mount left behind by a failed copype/dism operation.

        If copype or a manual dism mount fails partway through and its own cleanup/unmount
        also fails (confirmed: copype reporting "still mounted!" after a failed boot-file
        copy), the mount point registry can be left in a stuck state that blocks future
        WinPE builds. `dism /Cleanup-Mountpoints` is Microsoft's documented recovery command
        for exactly this situation.
        """
        if not self.check_not_busy():
            return
        if not is_admin():
            messagebox.showerror("Administrator Required", "Cleaning up mount points requires running as Administrator.")
            return

        if not messagebox.askyesno(
            "Clean Up Stuck WIM Mounts",
            "This runs 'dism /Cleanup-Mountpoints', which discards any orphaned/corrupted "
            "WIM mount left behind by a failed WinPE build.\n\n"
            "Only do this if a build actually failed with a \"still mounted\" error - it "
            "will discard any mount that's genuinely still in progress.\n\nContinue?"
        ):
            return

        self.log_message("Cleaning up stuck WIM mount points...", "INFO")
        self.set_operation_running(True)

        def worker():
            try:
                result = subprocess.run(
                    ['dism', '/Cleanup-Mountpoints'],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                detail = (result.stdout or result.stderr).strip()
                self.ui_queue.put(("log", detail or "No output", "INFO"))

                if result.returncode == 0:
                    self.ui_queue.put(("log", "✓ Mount points cleaned up - try building WinPE media again", "SUCCESS"))

                    def show_success():
                        messagebox.showinfo("Cleanup Complete", "Stuck mount points cleared. Try the WinPE build again.")
                    self.after(0, show_success)
                else:
                    self.ui_queue.put(("log", f"Cleanup failed: {detail}", "ERROR"))

                    def show_error():
                        messagebox.showerror("Cleanup Failed", f"Could not clean up mount points:\n{detail}")
                    self.after(0, show_error)

            except Exception as e:
                error_message = str(e)
                self.ui_queue.put(("log", f"Mount cleanup error: {error_message}", "ERROR"))
                logger.exception("WinPE mount cleanup failed")

                def show_error():
                    messagebox.showerror("Cleanup Failed", f"Failed to clean up mount points:\n{error_message}")
                self.after(0, show_error)

            finally:
                def reset_ui():
                    self.set_operation_running(False)
                self.after(0, reset_ui)

        threading.Thread(target=worker, daemon=True).start()

    def open_adk_download_page(self):
        """Open the official Microsoft Windows ADK / WinPE add-on download page in the default browser."""
        try:
            webbrowser.open(WINPE_ADK_DOWNLOAD_URL)
            self.log_message("Opened Windows ADK download page in browser", "INFO")
        except Exception as e:
            logger.exception("Failed to open ADK download page")
            messagebox.showerror(
                "Could Not Open Browser",
                f"Couldn't open the browser automatically.\n\nADK download page:\n{WINPE_ADK_DOWNLOAD_URL}"
            )

    def add_winpe_bundle_folder(self):
        """Add a folder (e.g. App Inventory / Environment Backup / DISM restore script) to bundle into WinPE media."""
        path = filedialog.askdirectory(title="Select Folder to Include in WinPE Media")
        if not path:
            return
        path = os.path.normpath(path)
        if path in self.winpe_bundle_folders:
            return
        self.winpe_bundle_folders.append(path)
        self.refresh_winpe_folder_list()

    def refresh_winpe_folder_list(self):
        """Redraw the list of folders queued to bundle into WinPE media."""
        for widget in self.winpe_folder_list_frame.winfo_children():
            widget.destroy()

        if not self.winpe_bundle_folders:
            ctk.CTkLabel(
                self.winpe_folder_list_frame,
                text="No folders added yet",
                font=("Arial", 10),
                text_color="gray60"
            ).pack(anchor="w", pady=5)
            return

        for folder in self.winpe_bundle_folders:
            row = ctk.CTkFrame(self.winpe_folder_list_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=f"📂 {folder}", font=("Arial", 10), anchor="w").pack(
                side="left", fill="x", expand=True
            )
            ctk.CTkButton(
                row, text="✕", width=28, height=24,
                fg_color="#E74C3C", hover_color="#C0392B",
                command=lambda f=folder: self.remove_winpe_bundle_folder(f)
            ).pack(side="right")

    def remove_winpe_bundle_folder(self, folder: str):
        """Remove a folder from the WinPE bundle list."""
        if folder in self.winpe_bundle_folders:
            self.winpe_bundle_folders.remove(folder)
        self.refresh_winpe_folder_list()

    def autodetect_winpe_app_exe(self):
        """Look for a compiled backuppro3.exe next to this script and pre-fill it if found."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(script_dir, "dist", "backuppro3.exe")

        if os.path.exists(candidate):
            self.winpe_app_path_entry.delete(0, tk.END)
            self.winpe_app_path_entry.insert(0, candidate)
            self.winpe_include_app.select()

        self.refresh_winpe_app_status()

    def refresh_winpe_app_status(self):
        """Warn if the selected exe looks older than the current source (needs a rebuild)."""
        exe_path = self.winpe_app_path_entry.get().strip()
        if not exe_path or not os.path.exists(exe_path):
            self.winpe_app_status_label.configure(
                text="No BackupPro.exe selected — build one with PyInstaller "
                     "(pyinstaller backuppro3.spec) first.",
                text_color="gray60"
            )
            return

        try:
            script_path = os.path.abspath(__file__)
            if os.path.exists(script_path) and os.path.getmtime(exe_path) < os.path.getmtime(script_path):
                self.winpe_app_status_label.configure(
                    text="⚠️ This .exe is older than backuppro3.py — rebuild it "
                         "(pyinstaller backuppro3.spec) so the WinPE copy has your latest features.",
                    text_color="#E67E22"
                )
            else:
                self.winpe_app_status_label.configure(
                    text=f"✓ {os.path.basename(exe_path)} looks up to date",
                    text_color="#27AE60"
                )
        except Exception:
            self.winpe_app_status_label.configure(text="")

    def browse_winpe_app_exe(self):
        """Browse for the compiled BackupPro executable to bundle onto WinPE media."""
        path = filedialog.askopenfilename(
            title="Select BackupPro Executable",
            filetypes=[("Executable", "*.exe"), ("All Files", "*.*")]
        )
        if path:
            path = os.path.normpath(path)
            self.winpe_app_path_entry.delete(0, tk.END)
            self.winpe_app_path_entry.insert(0, path)
            self.winpe_include_app.select()
        self.refresh_winpe_app_status()

    def get_removable_drives(self) -> List[str]:
        """Get list of removable (USB) drives on Windows."""
        drives = []
        if sys.platform == 'win32':
            DRIVE_REMOVABLE = 2
            for letter in 'DEFGHIJKLMNOPQRSTUVWXYZ':
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    try:
                        if ctypes.windll.kernel32.GetDriveTypeW(drive) == DRIVE_REMOVABLE:
                            drives.append(f"{letter}:")
                    except Exception:
                        pass
        return drives

    def resolve_unc_path(self, path: str) -> Optional[str]:
        """If path is on a mapped network drive letter, resolve it to its \\\\server\\share UNC form.

        wbAdmin flatly rejects mapped drive letters as a backup target - it needs the real
        network path. Reads the mapping from HKCU\\Network\\<letter> in the registry rather
        than asking the OS directly (GetDriveTypeW / `net use`), because elevated/Administrator
        processes run in a different logon session and can't see drive letters mapped by the
        normal user session - but the registry record of a persistent mapping is written to
        the same per-user hive regardless of elevation, so it's still readable there.
        Returns None if the path isn't on a mapped drive or no record exists.
        """
        drive, tail = os.path.splitdrive(path)
        if not drive:
            return None
        letter = drive.rstrip(':').upper()

        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"Network\\{letter}") as key:
                remote_path, _ = winreg.QueryValueEx(key, "RemotePath")
        except OSError:
            return None

        tail = tail.lstrip('\\/')
        return os.path.join(remote_path, tail) if tail else remote_path

    def get_saved_nas_username(self, path: str) -> str:
        """Look up the Windows username tied to a mapped drive's persistent connection, if any.

        Read from the registry for the same elevation-visibility reason as resolve_unc_path.
        Used only to pre-fill the NAS login prompt - never assumed correct.
        """
        drive, _ = os.path.splitdrive(path)
        if not drive:
            return ""
        letter = drive.rstrip(':').upper()
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"Network\\{letter}") as key:
                username, _ = winreg.QueryValueEx(key, "UserName")
                return username or ""
        except OSError:
            return ""

    def prompt_select_drive(self, drives: List[str]) -> Optional[str]:
        """Prompt the user to pick a drive letter from a list of candidates."""
        if len(drives) == 1:
            return drives[0]

        dialog = ctk.CTkToplevel(self)
        dialog.title("Select USB Drive")
        dialog.geometry("340x270")
        dialog.transient(self)
        dialog.grab_set()

        selected = {"drive": None}

        ctk.CTkLabel(dialog, text="Select the target USB drive:", font=("Arial", 12)).pack(pady=(20, 10))
        combo = ctk.CTkComboBox(dialog, values=drives, width=200)
        combo.set(drives[0])
        combo.pack(pady=5)

        def confirm():
            selected["drive"] = combo.get()
            dialog.destroy()

        ctk.CTkButton(dialog, text="Select", command=confirm).pack(pady=15)
        dialog.wait_window()
        return selected["drive"]

    def pick_winpe_workspace_location(self) -> Optional[str]:
        """Ask for a local-fixed-drive folder to build the WinPE workspace in.

        Refuses removable/network drives outright (rather than warning and letting the
        user continue anyway) because copype's boot-file extraction step has a confirmed
        0% success rate on them, even though the earlier WIM mount step works fine there.
        This is a separate concern from the final ISO/USB *output* location, which is
        unaffected and asked for later.
        """
        suggested = os.path.normpath(os.path.join(os.environ.get('TEMP', os.path.expanduser('~')), 'WinPE_Workspace'))

        while True:
            workspace_parent = filedialog.askdirectory(
                title="Select a LOCAL FIXED DRIVE Location for the WinPE Workspace (not USB/network - needs a few GB free)",
                initialdir=os.path.expanduser("~")
            )
            if not workspace_parent:
                return None
            # tkinter's dialogs return forward-slash paths on Windows, which copype/dism
            # fail to parse correctly when mixed with backslashes elsewhere in the path.
            workspace_parent = os.path.normpath(workspace_parent)

            try:
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(workspace_parent)[0] + "\\")
            except Exception:
                drive_type = 3  # assume fixed if we can't tell

            if drive_type not in (2, 4):  # not DRIVE_REMOVABLE or DRIVE_REMOTE
                return workspace_parent

            kind = "removable" if drive_type == 2 else "network"
            if messagebox.askyesno(
                "Removable/Network Drive Not Supported for the Workspace",
                f"{workspace_parent} is on a {kind} drive.\n\n"
                "copype's boot-file extraction step reliably fails there (confirmed "
                "repeatedly) even though the WIM mount step itself works fine. This must "
                "be a local fixed drive (C:\\ or another internal drive) - your USB/network "
                "drive is still used later for the final ISO/USB output, just not for this "
                f"temporary workspace.\n\nUse {suggested} instead?"
            ):
                os.makedirs(suggested, exist_ok=True)
                return suggested
            # else: loop back and ask again

    def build_winpe_media(self):
        """Build a bootable WinPE ISO or USB drive with the selected restore content bundled in."""
        if not self.check_not_busy():
            return
        adk = find_winpe_adk()
        if not adk:
            if messagebox.askyesno(
                "ADK Not Found",
                "The Windows ADK with the WinPE add-on was not found on this system.\n\n"
                "Install both the 'Windows ADK' and the 'Windows PE add-on' from Microsoft, then try again.\n\n"
                "Open the download page now?"
            ):
                self.open_adk_download_page()
            return

        if not is_admin():
            messagebox.showerror("Administrator Required", "Building WinPE media requires running as Administrator.")
            return

        arch = self.winpe_arch.get() or "amd64"
        output_type = self.winpe_output_type.get()
        add_ps = bool(self.winpe_add_powershell.get())
        cleanup = bool(self.winpe_cleanup.get())
        bundle_folders = list(self.winpe_bundle_folders)

        include_app = bool(self.winpe_include_app.get())
        app_exe_path = self.winpe_app_path_entry.get().strip()

        if include_app:
            if not app_exe_path or not os.path.exists(app_exe_path):
                messagebox.showerror(
                    "BackupPro.exe Not Found",
                    "'Include BackupPro on this WinPE media' is checked, but no valid .exe is selected.\n\n"
                    "Browse to a compiled backuppro3.exe, or uncheck the option to continue without it."
                )
                return

            script_path = os.path.abspath(__file__)
            if os.path.exists(script_path) and os.path.getmtime(app_exe_path) < os.path.getmtime(script_path):
                if not messagebox.askyesno(
                    "Executable May Be Outdated",
                    f"{os.path.basename(app_exe_path)} is older than the current backuppro3.py — it won't "
                    "have your latest features.\n\nBundle it anyway?"
                ):
                    return

        # Always ask where the build workspace should go - refuses removable/network
        # drives rather than just warning, since "continue anyway" has a confirmed 0%
        # success rate for copype's boot-file extraction step on this kind of drive.
        workspace_parent = self.pick_winpe_workspace_location()
        if not workspace_parent:
            return

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        workspace = os.path.join(workspace_parent, f"WinPE_Build_{timestamp}")

        usb_drive = None
        iso_path = None

        if output_type == "USB Drive":
            removable = self.get_removable_drives()
            if not removable:
                messagebox.showerror("No USB Drive Found", "No removable USB drive was detected. Insert one and try again.")
                return

            usb_drive = self.prompt_select_drive(removable)
            if not usb_drive:
                return

            confirm_text = simpledialog.askstring(
                "Confirm Drive Erase",
                f"⚠️ EVERYTHING on drive {usb_drive} will be PERMANENTLY ERASED.\n\n"
                f"Type the drive letter ({usb_drive[0]}) to confirm:"
            )
            if not confirm_text or confirm_text.strip().upper() != usb_drive[0].upper():
                messagebox.showinfo("Cancelled", "Drive confirmation did not match. Build cancelled.")
                return
        else:
            # Always ask where to save the ISO
            iso_path = filedialog.asksaveasfilename(
                title="Save WinPE Recovery ISO As",
                defaultextension=".iso",
                filetypes=[("ISO Image", "*.iso")],
                initialfile=f"REGTeches_Recovery_{timestamp}.iso"
            )
            if not iso_path:
                return
            iso_path = os.path.normpath(iso_path)

        self.log_message("Starting WinPE recovery media build...", "INFO")
        self.set_operation_running(True)

        def winpe_worker():
            setup_script = None
            media_script = None
            try:
                self.ui_queue.put(("log", f"Creating WinPE workspace ({arch})...", "INFO"))

                setup_lines = ["@echo off"]
                if adk.get("setenv"):
                    setup_lines.append(f'call "{adk["setenv"]}"')
                setup_lines.append(f'call "{adk["copype"]}" {arch} "{workspace}"')

                setup_script = os.path.join(workspace_parent, f"_winpe_setup_{timestamp}.bat")
                with open(setup_script, 'w', encoding='utf-8') as f:
                    f.write("\r\n".join(setup_lines) + "\r\n")

                result = subprocess.run(['cmd', '/c', setup_script], capture_output=True, text=True)
                if result.stdout:
                    self.ui_queue.put(("log", result.stdout.strip()[-1500:], "INFO"))

                boot_wim = os.path.join(workspace, "media", "sources", "boot.wim")
                if result.returncode != 0 or not os.path.exists(boot_wim):
                    self.ui_queue.put(("log", f"copype failed: {result.stderr.strip()[:300]}", "ERROR"))
                    raise RuntimeError("Failed to create WinPE workspace (copype). See log for details.")

                self.ui_queue.put(("log", "✓ WinPE workspace created", "SUCCESS"))

                mount_dir = os.path.join(workspace, "mount")
                os.makedirs(mount_dir, exist_ok=True)

                self.ui_queue.put(("log", "Mounting boot.wim...", "INFO"))
                result = subprocess.run(
                    ['dism', '/Mount-Image', f'/ImageFile:{boot_wim}', '/index:1', f'/MountDir:{mount_dir}'],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                if result.returncode != 0:
                    self.ui_queue.put(("log", f"DISM mount failed: {result.stderr.strip()[:300]}", "ERROR"))
                    raise RuntimeError("Failed to mount boot.wim")

                self.ui_queue.put(("log", "✓ boot.wim mounted", "SUCCESS"))

                # Optional components for PowerShell / DISM cmdlets support
                if add_ps:
                    oc_dir = os.path.join(adk["winpe_root"], arch, "WinPE_OCs")
                    packages = [
                        "WinPE-WMI.cab", "WinPE-NetFX.cab", "WinPE-Scripting.cab",
                        "WinPE-PowerShell.cab", "WinPE-DismCmdlets.cab", "WinPE-StorageWMI.cab",
                    ]
                    for pkg in packages:
                        pkg_path = os.path.join(oc_dir, pkg)
                        lang_path = os.path.join(oc_dir, "en-us", pkg.replace(".cab", "_en-us.cab"))
                        if os.path.exists(pkg_path):
                            self.ui_queue.put(("log", f"Adding optional component: {pkg}", "INFO"))
                            subprocess.run(
                                ['dism', f'/Image:{mount_dir}', '/Add-Package', f'/PackagePath:{pkg_path}'],
                                capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                            )
                            if os.path.exists(lang_path):
                                subprocess.run(
                                    ['dism', f'/Image:{mount_dir}', '/Add-Package', f'/PackagePath:{lang_path}'],
                                    capture_output=True, text=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                                )
                        else:
                            self.ui_queue.put(("log", f"Skipping {pkg} (not found in this ADK install)", "WARNING"))

                # Copy bundled restore content
                regteches_dir = os.path.join(mount_dir, "REGTeches")
                os.makedirs(regteches_dir, exist_ok=True)
                for folder in bundle_folders:
                    try:
                        dest = os.path.join(regteches_dir, os.path.basename(folder.rstrip("\\/")))
                        self.ui_queue.put(("log", f"Bundling {os.path.basename(folder)}...", "INFO"))
                        shutil.copytree(folder, dest)
                    except Exception as e:
                        self.ui_queue.put(("log", f"Failed to bundle {folder}: {str(e)[:150]}", "WARNING"))

                # Copy the BackupPro application itself so it can be run directly from WinPE
                app_bundled = False
                if include_app:
                    try:
                        self.ui_queue.put(("log", "Copying BackupPro application into WinPE media...", "INFO"))
                        app_dest_dir = os.path.join(regteches_dir, "BackupPro")
                        os.makedirs(app_dest_dir, exist_ok=True)
                        shutil.copy2(app_exe_path, os.path.join(app_dest_dir, "backuppro3.exe"))
                        app_bundled = True
                        self.ui_queue.put(("log", "✓ BackupPro application bundled", "SUCCESS"))
                    except Exception as e:
                        self.ui_queue.put(("log", f"Failed to bundle BackupPro.exe: {str(e)[:150]}", "WARNING"))

                # Startup menu (built dynamically so the BackupPro option only appears when bundled)
                menu_path = os.path.join(regteches_dir, "menu.cmd")
                menu_lines = [
                    "@echo off",
                    ":menu",
                    "cls",
                    "echo ============================================",
                    "echo   REGTeches Recovery Menu",
                    "echo ============================================",
                    "echo.",
                ]
                option = 1
                choice_handlers = []

                if app_bundled:
                    menu_lines.append(f"echo   {option}. Launch BackupPro (backup/restore tool)")
                    choice_handlers.append((option, 'start "" X:\\REGTeches\\BackupPro\\backuppro3.exe', True))
                    option += 1

                menu_lines.append(f"echo   {option}. Open a command prompt in the restore scripts folder")
                choice_handlers.append((option, 'cmd /k "cd /d X:\\REGTeches"', True))
                option += 1

                menu_lines.append(f"echo   {option}. Open a plain command prompt")
                choice_handlers.append((option, 'cmd', True))
                option += 1

                menu_lines.append(f"echo   {option}. Reboot")
                choice_handlers.append((option, 'wpeutil reboot', False))

                menu_lines.append("echo.")
                menu_lines.append("set /p choice=Select an option: ")
                for num, action, loop_back in choice_handlers:
                    if loop_back:
                        # Parens group both commands under the if - without them, "goto menu"
                        # would run unconditionally via & and every other option unreachable.
                        menu_lines.append(f'if "%choice%"=="{num}" ({action} & goto menu)')
                    else:
                        menu_lines.append(f'if "%choice%"=="{num}" {action}')
                menu_lines.append("goto menu")

                with open(menu_path, 'w', encoding='utf-8') as f:
                    f.write("\r\n".join(menu_lines) + "\r\n")

                startnet_path = os.path.join(mount_dir, "Windows", "System32", "startnet.cmd")
                if os.path.exists(startnet_path):
                    with open(startnet_path, 'a', encoding='utf-8') as f:
                        f.write("\r\nX:\\REGTeches\\menu.cmd\r\n")

                self.ui_queue.put(("log", "Unmounting and committing changes...", "INFO"))
                result = subprocess.run(
                    ['dism', '/Unmount-Image', f'/MountDir:{mount_dir}', '/Commit'],
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                if result.returncode != 0:
                    self.ui_queue.put(("log", f"DISM unmount failed: {result.stderr.strip()[:300]}", "ERROR"))
                    raise RuntimeError("Failed to unmount/commit boot.wim")

                self.ui_queue.put(("log", "✓ boot.wim committed", "SUCCESS"))

                # Build final media
                media_lines = ["@echo off"]
                # This is a fresh cmd.exe process, separate from the one that ran copype -
                # its PATH additions (oscdimg.exe etc.) don't carry over, so DandISetEnv
                # needs to run again here too (confirmed: 'oscdimg' is not recognized
                # without this, when building an ISO).
                if adk.get("setenv"):
                    media_lines.append(f'call "{adk["setenv"]}"')
                if usb_drive:
                    self.ui_queue.put(("log", f"Writing bootable media to {usb_drive} (this erases the drive)...", "INFO"))
                    media_lines.append(f'call "{adk["makewinpemedia"]}" /UFD "{workspace}" {usb_drive}')
                else:
                    self.ui_queue.put(("log", f"Building ISO: {iso_path}...", "INFO"))
                    media_lines.append(f'call "{adk["makewinpemedia"]}" /ISO "{workspace}" "{iso_path}"')

                media_script = os.path.join(workspace_parent, f"_winpe_media_{timestamp}.bat")
                with open(media_script, 'w', encoding='utf-8') as f:
                    f.write("\r\n".join(media_lines) + "\r\n")

                # /UFD asks an interactive "Proceed with Format [Y,N]?" confirmation before
                # writing to the USB drive - with no stdin, it reads EOF and aborts without
                # formatting ("UFD <drive> will not be formatted; exiting."). We already
                # confirmed the erase with the user via the type-the-drive-letter prompt
                # earlier, so answer it here.
                media_stdin = "Y\r\n" if usb_drive else None
                result = subprocess.run(['cmd', '/c', media_script], capture_output=True, text=True, input=media_stdin)
                # MakeWinPEMedia (like dism/wbadmin) writes its meaningful output to stdout,
                # not stderr - combine both so an error message is never silently blank.
                media_output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
                if media_output:
                    self.ui_queue.put(("log", media_output[-1500:], "INFO"))

                if result.returncode != 0:
                    detail = media_output[-500:] if media_output else f"exit code {result.returncode}"
                    self.ui_queue.put(("log", f"MakeWinPEMedia failed: {detail}", "ERROR"))
                    raise RuntimeError(f"Failed to build final WinPE media:\n{detail}")

                # MakeWinPEMedia can report success (exit code 0) even when its internal
                # diskpart/bootsect step silently failed, leaving files copied onto the
                # drive without actually making it bootable - verify the expected boot
                # markers exist before trusting the exit code alone.
                usb_verified = True
                if usb_drive:
                    drive_root = usb_drive if usb_drive.endswith('\\') else usb_drive + '\\'
                    boot_markers = [
                        os.path.join(drive_root, 'bootmgr'),
                        os.path.join(drive_root, 'efi', 'boot', 'bootx64.efi'),
                        os.path.join(drive_root, 'Boot', 'BCD'),
                    ]
                    usb_verified = any(os.path.exists(m) for m in boot_markers)
                    if not usb_verified:
                        self.ui_queue.put((
                            "log",
                            f"⚠️ {usb_drive} doesn't show the expected boot files (bootmgr, "
                            "efi\\boot\\bootx64.efi, Boot\\BCD) even though MakeWinPEMedia "
                            "reported success - files may have been copied without the "
                            "drive actually being made bootable.",
                            "WARNING"
                        ))

                self.ui_queue.put(("log", "═══════════════════════════════", "INFO"))
                self.ui_queue.put(("log", "WinPE Recovery Media Build Complete!", "SUCCESS"))

                if cleanup:
                    self.ui_queue.put(("log", "Cleaning up build workspace...", "INFO"))
                    shutil.rmtree(workspace, ignore_errors=True)

                result_location = usb_drive if usb_drive else iso_path

                self.send_notification(
                    "WinPE Media Build Complete",
                    f"Recovery media build finished.\nLocation: {result_location}"
                )

                def show_completion():
                    if not usb_verified:
                        messagebox.showwarning(
                            "Build Finished, But Not Verified as Bootable",
                            f"MakeWinPEMedia reported success writing to {usb_drive}, but the "
                            "expected boot files weren't found on the drive - it may not "
                            "actually boot.\n\n"
                            "Try instead: build with ISO output, then write that ISO to a USB "
                            "drive with a dedicated tool like Rufus (rufus.ie), which is more "
                            "reliable for creating bootable USB media than MakeWinPEMedia's "
                            "/UFD option in some environments."
                        )
                        return
                    messagebox.showinfo(
                        "WinPE Media Build Complete",
                        f"Recovery media created successfully!\n\nLocation: {result_location}\n\n"
                        "Boot from this media on the target PC to run your bundled restore scripts."
                    )
                    if iso_path and messagebox.askyesno("Open Folder", "Open the folder containing the ISO?"):
                        os.startfile(os.path.dirname(iso_path))

                self.after(0, show_completion)

            except Exception as e:
                error_message = str(e)
                self.ui_queue.put(("log", f"WinPE build error: {error_message}", "ERROR"))
                logger.exception("WinPE media build failed")

                def show_error():
                    messagebox.showerror("Build Error", f"Failed to build WinPE media:\n{error_message}")
                self.after(0, show_error)

            finally:
                for temp_script in (setup_script, media_script):
                    if temp_script and os.path.exists(temp_script):
                        try:
                            os.remove(temp_script)
                        except Exception:
                            pass

                def reset_ui():
                    self.set_operation_running(False)
                self.after(0, reset_ui)

        threading.Thread(target=winpe_worker, daemon=True).start()

    def build_restore_tab(self):
        """Build the Restore tab for recovering backed up data."""
        tab = self.tabs.tab("Restore")
        
        # Header
        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkLabel(
            header,
            text="🔄 Restore Wizard",
            font=("Arial", 16, "bold")
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            header,
            text="Browse and restore files from your backups",
            font=("Arial", 11),
            text_color="gray70"
        ).pack(anchor="w", pady=(5, 0))
        
        # Select backup folder
        self.restore_backup_path = self.create_path_selector(
            tab,
            "📂 Backup Location:",
            "Select the backup folder to restore from"
        )
        
        # Scan button
        scan_frame = ctk.CTkFrame(tab, fg_color="transparent")
        scan_frame.pack(fill="x", padx=40, pady=10)
        
        ctk.CTkButton(
            scan_frame,
            text="🔍 Scan for Backups",
            font=("Arial", 12, "bold"),
            fg_color="#3498DB",
            hover_color="#2980B9",
            height=40,
            command=self.scan_backups
        ).pack(fill="x")
        
        # Backup list
        list_frame = ctk.CTkFrame(tab)
        list_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        ctk.CTkLabel(
            list_frame,
            text="Available Backups:",
            font=("Arial", 12, "bold")
        ).pack(anchor="w", padx=10, pady=10)
        
        self.restore_list_scroll = ctk.CTkScrollableFrame(list_frame, fg_color="transparent")
        self.restore_list_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        # Placeholder message
        ctk.CTkLabel(
            self.restore_list_scroll,
            text="👆 Select a backup location and click 'Scan for Backups' to see available backups",
            font=("Arial", 11),
            text_color="gray60"
        ).pack(pady=30)
    
    # ==================== UI HELPER METHODS ====================
    
    def create_path_selector(self, master, label_text: str, placeholder: str = "") -> ctk.CTkEntry:
        """Create a path selector with label, entry, and browse button."""
        container = ctk.CTkFrame(master, fg_color="transparent")
        container.pack(fill="x", padx=40, pady=10)
        
        ctk.CTkLabel(
            container,
            text=label_text,
            font=("Arial", 12, "bold")
        ).pack(anchor="w", pady=(0, 5))
        
        entry_frame = ctk.CTkFrame(container, fg_color="transparent")
        entry_frame.pack(fill="x")
        
        entry = ctk.CTkEntry(
            entry_frame,
            placeholder_text=placeholder,
            height=35
        )
        entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        ctk.CTkButton(
            entry_frame,
            text="📁 Browse",
            width=100,
            command=lambda: self.browse_folder(entry)
        ).pack(side="right")
        
        return entry
    
    def browse_folder(self, entry: ctk.CTkEntry):
        """Open folder browser dialog."""
        path = filedialog.askdirectory(title="Select Folder")
        if path:
            # tkinter's file dialogs return forward-slash paths on Windows (e.g. "I:/Foo"),
            # which some external tools (confirmed: DISM's imaging provider) fail to parse
            # correctly when mixed with backslashes elsewhere in the same path string.
            path = os.path.normpath(path)
            entry.delete(0, tk.END)
            entry.insert(0, path)
    
    def get_available_drives(self) -> List[str]:
        """Get list of available drives on Windows."""
        drives = []
        if sys.platform == 'win32':
            for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                drive = f"{letter}:"
                if os.path.exists(drive):
                    drives.append(drive)
        return drives if drives else ["C:", "D:"]
    
    # ==================== OPERATION HANDLERS ====================
    
    def run_sync(self):
        """Execute folder sync operation."""
        if not self.check_not_busy():
            return
        source = self.sync_src.get().strip()
        destination = self.sync_dst.get().strip()
        dry_run = self.sync_dryrun.get()
        compress = self.sync_compress.get()
        
        # Validation
        if not source or not destination:
            messagebox.showwarning("Missing Information", "Please specify both source and destination folders.")
            return
        
        if not validate_path(source):
            messagebox.showerror("Invalid Path", f"Source path does not exist or is invalid:\n{source}")
            return
        
        # Confirmation for non-dry-run
        if not dry_run:
            if compress:
                msg = (
                    f"📦 CREATE ZIP ARCHIVE\n\n"
                    f"Source: {source}\n"
                    f"Destination: {destination}\n\n"
                    f"A compressed ZIP archive will be created.\n\n"
                    f"Continue?"
                )
            else:
                msg = (
                    f"⚠️ WARNING: This will MIRROR the destination to match the source.\n\n"
                    f"Source: {source}\n"
                    f"Destination: {destination}\n\n"
                    f"Files in destination that don't exist in source will be DELETED.\n\n"
                    f"Continue?"
                )
            if not messagebox.askyesno("Confirm Sync", msg, icon='warning'):
                return
        
        # Disable UI during operation
        self.set_operation_running(True)
        
        # Run in thread
        self.current_operation_thread = threading.Thread(
            target=self._sync_worker,
            args=(source, destination, dry_run, compress),
            daemon=True
        )
        self.current_operation_thread.start()
    
    def _sync_worker(self, source: str, destination: str, dry_run: bool, compress: bool = False):
        """Worker thread for sync operation."""
        try:
            if compress and not dry_run:
                # Create ZIP archive
                self.ui_queue.put(("log", "Creating compressed ZIP archive...", "INFO"))
                self.ui_queue.put(("progress", 0.1))
                
                # Create timestamped zip filename
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                source_name = os.path.basename(source.rstrip('/\\'))
                zip_filename = f"{source_name}_{timestamp}.zip"
                zip_path = os.path.join(destination, zip_filename)
                
                # Ensure destination exists
                os.makedirs(destination, exist_ok=True)
                
                # Create the archive
                import zipfile
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    total_files = sum(1 for _ in Path(source).rglob('*') if _.is_file())
                    processed = 0
                    
                    self.ui_queue.put(("log", f"Compressing {total_files} files...", "INFO"))
                    
                    for root, dirs, files in os.walk(source):
                        # Skip certain folders
                        dirs[:] = [d for d in dirs if d not in ['node_modules', '.git', '__pycache__', 'Cache', 'cache']]
                        
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, source)
                            
                            try:
                                zipf.write(file_path, arcname)
                                processed += 1
                                
                                if processed % 100 == 0:
                                    progress = 0.1 + (processed / total_files * 0.8)
                                    self.ui_queue.put(("progress", progress))
                            except Exception as e:
                                self.ui_queue.put(("log", f"Warning: Could not add {file}: {str(e)[:50]}", "WARNING"))
                
                # Get archive size
                archive_size = os.path.getsize(zip_path) / (1024 * 1024)  # MB
                
                self.ui_queue.put(("progress", 1.0))
                self.ui_queue.put(("log", f"✓ Archive created: {zip_filename}", "SUCCESS"))
                self.ui_queue.put(("log", f"Archive size: {archive_size:.1f} MB", "SUCCESS"))
                self.ui_queue.put(("log", f"Location: {zip_path}", "SUCCESS"))
                
                # Send notification
                self.send_notification(
                    "Archive Complete",
                    f"Created compressed archive:\n{zip_filename}\nSize: {archive_size:.1f} MB"
                )
                
                def show_completion():
                    if messagebox.askyesno("Archive Complete", 
                                          f"Archive created successfully!\n\n"
                                          f"File: {zip_filename}\n"
                                          f"Size: {archive_size:.1f} MB\n\n"
                                          f"Open folder?"):
                        os.startfile(destination)
                
                self.after(0, show_completion)
                
            else:
                # Regular sync
                success = self.engine.sync_folders(source, destination, dry_run)
                
                if success and not dry_run:
                    # Send notification if enabled
                    self.send_notification(
                        "Sync Complete",
                        f"Successfully synced:\n{source}\n→ {destination}"
                    )
        except Exception as e:
            self.ui_queue.put(("log", f"Operation error: {str(e)}", "ERROR"))
            logger.exception("Sync/compress operation failed")
        finally:
            self.after(0, lambda: self.set_operation_running(False))
    
    def run_image(self):
        """Execute disk imaging operation."""
        if not self.check_not_busy():
            return
        if not is_admin():
            messagebox.showerror(
                "Admin Required",
                "Disk imaging requires administrator privileges.\n\n"
                "Please restart the application as Administrator."
            )
            return
        
        drive = self.img_drive.get()
        destination = self.img_dest.get().strip()
        keep_count = int(self.img_keep.get())

        # Validation
        if not destination:
            messagebox.showwarning("Missing Information", "Please specify a destination for the system image.")
            return

        # wbAdmin rejects mapped drive letters as a backup target - it needs the actual
        # network path. Detect this up front and offer the UNC equivalent instead of
        # letting wbAdmin fail deep into the operation.
        unc_path = self.resolve_unc_path(destination)
        suggested_username = self.get_saved_nas_username(destination) if unc_path else ""
        if unc_path:
            if messagebox.askyesno(
                "Mapped Network Drive Detected",
                f"{destination} is a mapped network drive.\n\n"
                "wbAdmin (Windows Backup) does not accept mapped drive letters as a backup "
                f"target — it needs the real network path.\n\nUse this instead?\n{unc_path}"
            ):
                destination = unc_path
            else:
                return

        nas_username = ""
        nas_password = ""
        is_network_target = destination.startswith('\\\\')

        if is_network_target:
            # Elevated/Administrator processes often can't see the normal user's mapped-drive
            # credentials, so os.path.exists() on a UNC path can fail even when the share is
            # fine - skip the local-path existence check and authenticate explicitly instead.
            login = self.prompt_nas_login(suggested_username=suggested_username)
            if login is None:
                return
            nas_username, nas_password = login
        elif not validate_path(destination):
            messagebox.showerror("Invalid Path", f"Destination path does not exist or is invalid:\n{destination}")
            return

        # Confirmation
        msg = (
            f"Create system image?\n\n"
            f"Drive: {drive}\n"
            f"Destination: {destination}\n"
            f"Keep last: {keep_count} backups\n\n"
            f"This may take 30+ minutes.\n\n"
            f"Continue?"
        )
        if not messagebox.askyesno("Confirm Imaging", msg):
            return

        # Disable UI during operation
        self.set_operation_running(True)

        # Run in thread
        self.current_operation_thread = threading.Thread(
            target=self._image_worker,
            args=(drive, destination, keep_count, nas_username, nas_password),
            daemon=True
        )
        self.current_operation_thread.start()

    def prompt_nas_login(self, suggested_username: str = "") -> Optional[tuple]:
        """Prompt for NAS credentials for a network (UNC) backup target.

        Pre-fills from Settings if previously saved, falling back to suggested_username
        (read from the mapped drive's own registry record) if nothing is saved yet.
        Returns (username, password), or None if the user cancels.
        """
        settings = self.config.get("settings", {})

        dialog = ctk.CTkToplevel(self)
        dialog.title("NAS Login Required")
        dialog.geometry("460x540")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text="🔐 NAS Login Required", font=("Arial", 14, "bold")).pack(pady=(15, 5))
        ctk.CTkLabel(
            dialog,
            text="Elevated backups can't see your normal mapped-drive login,\n"
                 "so wbAdmin needs the NAS credentials directly.",
            font=("Arial", 9), text_color="gray60", justify="center"
        ).pack(pady=(0, 10))

        form = ctk.CTkFrame(dialog, fg_color="transparent")
        form.pack(fill="x", padx=25)

        ctk.CTkLabel(form, text="Username:", font=("Arial", 11)).pack(anchor="w")
        user_entry = ctk.CTkEntry(form)
        user_entry.insert(0, settings.get('nas_username') or suggested_username)
        user_entry.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(form, text="Password:", font=("Arial", 11)).pack(anchor="w")
        pass_entry = ctk.CTkEntry(form, show="*")
        pass_entry.insert(0, settings.get('nas_password', ''))
        pass_entry.pack(fill="x", pady=(0, 8))

        remember_box = ctk.CTkCheckBox(form, text="Remember in Settings")
        if settings.get('nas_username'):
            remember_box.select()
        remember_box.pack(anchor="w", pady=(0, 5))

        status_lbl = ctk.CTkLabel(dialog, text="", font=("Arial", 9), text_color="#E67E22")
        status_lbl.pack()

        result = {"value": None}

        def do_login():
            username = user_entry.get().strip()
            password = pass_entry.get()
            if not username:
                status_lbl.configure(text="Enter a username")
                return
            if remember_box.get():
                self.config["settings"]["nas_username"] = username
                self.config["settings"]["nas_password"] = password
                self.save_config()
            result["value"] = (username, password)
            dialog.destroy()

        def do_cancel():
            dialog.destroy()

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=15)
        ctk.CTkButton(btn_row, text="Login", command=do_login).pack(side="left", padx=5)
        ctk.CTkButton(btn_row, text="Cancel", command=do_cancel).pack(side="left", padx=5)

        user_entry.focus_set()
        dialog.wait_window()
        return result["value"]


    def _image_worker(self, drive: str, destination: str, keep_count: int,
                       nas_username: str = "", nas_password: str = ""):
        """Worker thread for imaging operation."""
        try:
            success = self.engine.create_disk_image(drive, destination, keep_count, nas_username, nas_password)

            if success:
                # Send notification if enabled
                self.send_notification(
                    "Imaging Complete",
                    f"System image created:\nDrive {drive} → {destination}"
                )
        finally:
            self.after(0, lambda: self.set_operation_running(False))

    def reset_backup_service(self):
        """Restart the Windows Block Level Backup Engine service (wbengine).

        Fixes wbAdmin's "Another backup or recovery operation is in progress" error
        when no real backup is actually running - the usual cause is a prior wbAdmin
        run getting interrupted (cancelled, app closed, process killed) without
        cleanly releasing its lock. This is the standard, documented recovery step.
        """
        if not self.check_not_busy():
            return
        if not is_admin():
            messagebox.showerror(
                "Administrator Required",
                "Restarting the backup service requires running as Administrator."
            )
            return

        if not messagebox.askyesno(
            "Reset Backup Service",
            "This restarts the Windows Block Level Backup Engine service (wbengine), "
            "which clears wbAdmin's \"another backup is in progress\" error when nothing "
            "is actually running.\n\n"
            "Only do this if you're sure no backup is genuinely in progress right now — "
            "it will interrupt one if there is.\n\nContinue?"
        ):
            return

        self.log_message("Restarting backup service (wbengine)...", "INFO")
        self.set_operation_running(True)

        def worker():
            try:
                stop_result = subprocess.run(
                    ['net', 'stop', 'wbengine'], capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                self.ui_queue.put(("log", (stop_result.stdout or stop_result.stderr).strip() or "Service was not running", "INFO"))

                start_result = subprocess.run(
                    ['net', 'start', 'wbengine'], capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                detail = (start_result.stdout or start_result.stderr).strip()
                self.ui_queue.put(("log", detail or "Service restarted", "INFO"))

                if start_result.returncode == 0:
                    self.ui_queue.put(("log", "✓ Backup service reset - try CREATE SYSTEM IMAGE again", "SUCCESS"))

                    def show_success():
                        messagebox.showinfo("Backup Service Reset", "wbengine restarted. Try the backup again.")
                    self.after(0, show_success)
                else:
                    self.ui_queue.put(("log", f"Failed to restart wbengine: {detail}", "ERROR"))

                    def show_error():
                        messagebox.showerror("Reset Failed", f"Could not restart wbengine:\n{detail}")
                    self.after(0, show_error)

            except Exception as e:
                error_message = str(e)
                self.ui_queue.put(("log", f"Backup service reset error: {error_message}", "ERROR"))
                logger.exception("Backup service reset failed")

                def show_error():
                    messagebox.showerror("Reset Failed", f"Failed to reset backup service:\n{error_message}")
                self.after(0, show_error)

            finally:
                def reset_ui():
                    self.set_operation_running(False)
                self.after(0, reset_ui)

        threading.Thread(target=worker, daemon=True).start()

    def capture_dism_image(self):
        """Capture a folder/drive into a DISM .wim image (alternative to wbAdmin)."""
        if not self.check_not_busy():
            return
        if not is_admin():
            messagebox.showerror("Administrator Required", "DISM image capture requires running as Administrator.")
            return

        source = filedialog.askdirectory(
            title="Select Folder or Drive to Capture (e.g. D:\\ or an offline OS partition)"
        )
        if not source:
            return
        # DISM fails to parse a mixed-separator path (tkinter dialogs return forward
        # slashes on Windows) - see build_winpe_media for the confirmed dism.log error.
        source = os.path.normpath(source)

        dest_wim = filedialog.asksaveasfilename(
            title="Save WIM Image As",
            defaultextension=".wim",
            filetypes=[("Windows Image", "*.wim")],
            initialfile=f"SystemImage_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.wim"
        )
        if not dest_wim:
            return
        dest_wim = os.path.normpath(dest_wim)

        if os.path.normcase(os.path.abspath(dest_wim)).startswith(os.path.normcase(os.path.abspath(source)) + os.sep):
            messagebox.showerror("Invalid Destination", "The image file cannot be saved inside the folder being captured.")
            return

        system_drive = os.environ.get('SystemDrive', 'C:') + '\\'
        capturing_live_os = os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(system_drive))
        use_exclusions = False

        if capturing_live_os:
            use_exclusions = messagebox.askyesno(
                "Capturing the Live System Drive",
                f"You're about to capture {system_drive} while Windows is running from it.\n\n"
                "DISM's /Capture-Image is not VSS-aware like wbAdmin — it commonly fails on files "
                "that are locked while Windows is running (pagefile.sys, hiberfil.sys, swapfile.sys, "
                "DumpStack.log.tmp, System Volume Information).\n\n"
                "For a reliable full OS image, boot into WinPE first (see the WinPE Media tab) and "
                "capture from there instead.\n\n"
                "Continue anyway with common lock-prone files excluded? (reduces but doesn't "
                "eliminate the risk of failure)"
            )
            if not use_exclusions:
                return

        image_name = simpledialog.askstring(
            "Image Name", "Enter a name for this image:", initialvalue="System Backup"
        ) or "System Backup"

        msg = (
            f"Capture DISM image?\n\n"
            f"Source: {source}\n"
            f"Destination: {dest_wim}\n\n"
            f"This may take a long time depending on data size.\n\nContinue?"
        )
        if not messagebox.askyesno("Confirm DISM Capture", msg):
            return

        self.log_message("Starting DISM image capture...", "INFO")
        self.set_operation_running(True)

        def capture_worker():
            config_path = None
            try:
                dism_args = ['dism', '/Capture-Image', f'/ImageFile:{dest_wim}', f'/CaptureDir:{source}',
                             f'/Name:{image_name}', '/Compress:max', '/CheckIntegrity']

                if use_exclusions:
                    config_path = os.path.join(
                        os.environ.get('TEMP', '.'), f"_dism_exclude_{datetime.datetime.now().strftime('%H%M%S')}.ini"
                    )
                    with open(config_path, 'w', encoding='utf-8') as f:
                        f.write(
                            "[ExclusionList]\n"
                            "\\pagefile.sys\n"
                            "\\hiberfil.sys\n"
                            "\\swapfile.sys\n"
                            "\\DumpStack.log.tmp\n"
                            "\\DumpStack.log\n"
                            "\\System Volume Information\n"
                            "\\$Recycle.Bin\n"
                            "\\Windows\\Temp\n"
                            "\\Windows\\CSC\n"
                            "\\Recovery\n"
                            "\\OneDriveTemp\n"
                            "[CompressionExclusionList]\n"
                        )
                    dism_args.append(f'/ConfigFile:{config_path}')
                    self.ui_queue.put(("log", "Using exclusion list for common locked files...", "INFO"))

                self.ui_queue.put(("log", f"Capturing {source} to {dest_wim} (this can take a while)...", "INFO"))
                process = subprocess.Popen(
                    dism_args,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )

                # Drain both pipes live so long captures don't sit on a frozen progress bar,
                # and so a full pipe buffer can't block DISM mid-write on a big capture.
                output_chunks = []

                def drain(stream, parse_progress=False):
                    if not stream:
                        return
                    for line in iter(stream.readline, ''):
                        output_chunks.append(line)
                        if parse_progress:
                            match = re.search(r'(\d{1,3})\s*%', line)
                            if match:
                                pct = min(int(match.group(1)), 100) / 100
                                self.ui_queue.put(("progress", pct))
                                self.ui_queue.put(("status", f"Capturing... {match.group(1)}%"))
                    stream.close()

                stdout_thread = threading.Thread(target=drain, args=(process.stdout, True), daemon=True)
                stderr_thread = threading.Thread(target=drain, args=(process.stderr, False), daemon=True)
                stdout_thread.start()
                stderr_thread.start()
                process.wait()
                stdout_thread.join(timeout=5)
                stderr_thread.join(timeout=5)

                if process.returncode != 0:
                    # DISM writes its actual error text to stdout (after the progress lines),
                    # not stderr - combine both so nothing gets silently dropped either way.
                    detail = "".join(output_chunks).strip()
                    detail = detail[-800:] if detail else f"exit code {process.returncode}"
                    self.ui_queue.put(("log", f"DISM capture failed:\n{detail}", "ERROR"))
                    raise RuntimeError(f"DISM capture failed:\n{detail}")

                self.ui_queue.put(("log", "═══════════════════════════════", "INFO"))
                self.ui_queue.put(("log", "DISM Image Capture Complete!", "SUCCESS"))
                self.ui_queue.put(("log", f"Image: {dest_wim}", "SUCCESS"))

                self.send_notification("DISM Image Capture Complete", f"Image saved to {dest_wim}")

                def show_completion():
                    messagebox.showinfo(
                        "DISM Capture Complete",
                        f"Image captured successfully!\n\nFile: {dest_wim}\n\n"
                        "Use 'Generate DISM Restore Script' to create the apply script for a "
                        "clean-install restore, then bundle it into WinPE media on the WinPE Media tab."
                    )
                    if messagebox.askyesno("Open Folder", "Open the folder containing the image?"):
                        os.startfile(os.path.dirname(dest_wim))

                self.after(0, show_completion)

            except Exception as e:
                error_message = str(e)
                self.ui_queue.put(("log", f"DISM capture error: {error_message}", "ERROR"))
                logger.exception("DISM capture failed")

                def show_error():
                    hint = ""
                    if "cannot access the file because it is being used" in error_message.lower():
                        hint = (
                            "\n\nThis usually means some other running process has a file open "
                            "somewhere on the drive - not necessarily one of the excluded files. "
                            "Live capture of an in-use drive can hit this on any locked file; "
                            "wbAdmin (the CREATE SYSTEM IMAGE button above) uses Volume Shadow Copy "
                            "and doesn't have this problem, or capture from WinPE instead."
                        )
                    dism_log = r"C:\Windows\Logs\DISM\dism.log"
                    if messagebox.askyesno(
                        "Capture Error",
                        f"Failed to capture image:\n{error_message}{hint}\n\n"
                        f"Open {dism_log} to see exactly which file caused it?"
                    ):
                        if os.path.exists(dism_log):
                            os.startfile(dism_log)
                        else:
                            messagebox.showwarning("Not Found", f"{dism_log} does not exist.")
                self.after(0, show_error)

            finally:
                if config_path and os.path.exists(config_path):
                    try:
                        os.remove(config_path)
                    except Exception:
                        pass

                def reset_ui():
                    self.set_operation_running(False)
                self.after(0, reset_ui)

        threading.Thread(target=capture_worker, daemon=True).start()

    def generate_dism_restore_script(self):
        """Generate a DISM apply-image script for bare-metal restore, meant to be run from WinPE."""
        wim_path = filedialog.askopenfilename(
            title="Select the WIM Image This Script Will Restore",
            filetypes=[("Windows Image", "*.wim"), ("All Files", "*.*")]
        )
        if not wim_path:
            return
        wim_path = os.path.normpath(wim_path)

        destination = filedialog.askdirectory(
            title="Select Where to Save the Restore Script",
            initialdir=os.path.expanduser("~")
        )
        if not destination:
            return
        destination = os.path.normpath(destination)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_folder = os.path.join(destination, f"DismRestore_{timestamp}")

        try:
            os.makedirs(out_folder, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot create folder:\n{e}")
            return

        wim_filename = os.path.basename(wim_path)

        script_path = os.path.join(out_folder, "dism_apply.ps1")
        with open(script_path, 'w', encoding='utf-8') as f:
            f.write(
                "# REGTeches DISM Restore Script - RUN FROM WinPE ON THE TARGET MACHINE\n"
                f"# Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# Source image: {wim_filename}\n"
                "#\n"
                "# REVIEW EVERY LINE BEFORE RUNNING - this restores a full OS image and can\n"
                "# involve formatting a disk. Edit the placeholders below (drive letters) to\n"
                "# match the target hardware; they will differ from machine to machine.\n"
                "#\n"
                "# Expects the .wim file to sit next to this script (copy it in before\n"
                "# bundling this folder into your WinPE media).\n\n"
                f"$ImagePath = Join-Path $PSScriptRoot \"{wim_filename}\"\n"
                "$TargetDrive = \"W:\"      # EDIT ME: drive letter assigned to the Windows partition after formatting\n"
                "$SystemDrive = \"S:\"      # EDIT ME: drive letter assigned to the EFI/System partition\n\n"
                "# --- Example diskpart layout for a UEFI/GPT disk (reference only - run diskpart\n"
                "# separately first, and double-check the disk number before using 'clean'):\n"
                "#   select disk 0\n"
                "#   clean\n"
                "#   convert gpt\n"
                "#   create partition efi size=100\n"
                "#   format quick fs=fat32 label=\"System\"\n"
                "#   assign letter=S\n"
                "#   create partition msr size=16\n"
                "#   create partition primary\n"
                "#   format quick fs=ntfs label=\"Windows\"\n"
                "#   assign letter=W\n\n"
                "if (-not (Test-Path $ImagePath)) {\n"
                "    Write-Host \"Image not found at $ImagePath - copy the .wim next to this script first.\" -ForegroundColor Red\n"
                "    exit 1\n"
                "}\n\n"
                "Write-Host \"Applying image to $TargetDrive ...\" -ForegroundColor Cyan\n"
                "dism /Apply-Image /ImageFile:$ImagePath /Index:1 /ApplyDir:$TargetDrive\\\n\n"
                "Write-Host \"Making $TargetDrive bootable...\" -ForegroundColor Cyan\n"
                "bcdboot $TargetDrive\\Windows /s $SystemDrive\\ /f UEFI\n\n"
                "Write-Host \"Done. Remove the recovery media and reboot.\" -ForegroundColor Green\n"
            )

        restore_bat = os.path.join(out_folder, "dism_apply.bat")
        with open(restore_bat, 'w', encoding='utf-8') as f:
            f.write(
                "@echo off\r\n"
                "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0dism_apply.ps1\"\r\n"
                "pause\r\n"
            )

        self.log_message(f"DISM restore script created: {script_path}", "SUCCESS")

        messagebox.showinfo(
            "Restore Script Created",
            f"Script saved to:\n{out_folder}\n\n"
            "IMPORTANT:\n"
            "1. Copy your .wim image file into this same folder (next to the script).\n"
            "2. Open dism_apply.ps1 and edit the drive-letter placeholders for the target machine.\n"
            "3. Add this folder in the WinPE Media tab to bundle it into your recovery ISO/USB."
        )
        if messagebox.askyesno("Open Folder", "Open the folder now?"):
            os.startfile(out_folder)

    def cancel_operation(self):
        """Cancel the current running operation."""
        if self.engine.is_running:
            if messagebox.askyesno("Cancel Operation", "Are you sure you want to cancel the current operation?"):
                self.engine.cancel_operation()
    
    def quick_user_backup(self):
        """Quick backup of the current user's profile to a selected destination."""
        if not self.check_not_busy():
            return
        try:
            # Get current user's profile directory
            username = os.getenv('USERNAME') or os.getenv('USER')
            if sys.platform == 'win32':
                user_profile = os.path.join('C:\\Users', username)
            else:
                user_profile = os.path.expanduser('~')
            
            if not os.path.exists(user_profile):
                messagebox.showerror("Profile Not Found", f"Could not locate user profile:\n{user_profile}")
                return
            
            # Show confirmation dialog with user info
            msg = (
                f"🏠 Quick User Profile Backup\n\n"
                f"Current User: {username}\n"
                f"Profile Location: {user_profile}\n\n"
                f"This will backup:\n"
                f"• Desktop (including OneDrive)\n"
                f"• Documents (including OneDrive)\n"
                f"• Pictures (including OneDrive)\n"
                f"• Videos\n"
                f"• Downloads\n"
                f"• Music\n"
                f"• Favorites\n"
                f"• Saved Games\n\n"
                f"Select a destination folder for the backup."
            )
            
            if not messagebox.askyesno("Quick Profile Backup", msg, icon='question'):
                return
            
            # Ask for destination
            destination = filedialog.askdirectory(
                title="Select Backup Destination",
                initialdir=os.path.expanduser("~")
            )

            if not destination:
                return  # User cancelled
            destination = os.path.normpath(destination)

            # Create timestamped backup folder
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_folder = os.path.join(destination, f"UserBackup_{username}_{timestamp}")
            
            try:
                os.makedirs(backup_folder, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Cannot create backup folder:\n{e}")
                return
            
            # Confirm start
            confirm_msg = (
                f"✓ Ready to backup!\n\n"
                f"Source: {user_profile}\n"
                f"Destination: {backup_folder}\n\n"
                f"This may take several minutes depending on data size.\n\n"
                f"Start backup now?"
            )
            
            if not messagebox.askyesno("Confirm Backup", confirm_msg):
                return
            
            self.log_message(f"Starting quick user profile backup for: {username}", "INFO")
            self.log_message(f"Destination: {backup_folder}", "INFO")
            
            # Set operation as running
            self.set_operation_running(True)
            
            # Run backup in thread
            def backup_worker():
                try:
                    def find_folder_path(folder_name):
                        """Find the actual path of a user folder, checking OneDrive and standard locations."""
                        possible_paths = []
                        
                        # Standard location
                        standard_path = os.path.join(user_profile, folder_name)
                        if os.path.exists(standard_path):
                            possible_paths.append(standard_path)
                        
                        # OneDrive locations
                        onedrive_personal = os.path.join(user_profile, 'OneDrive', folder_name)
                        if os.path.exists(onedrive_personal):
                            possible_paths.append(onedrive_personal)
                        
                        onedrive_business = os.path.join(user_profile, 'OneDrive - *', folder_name)
                        import glob
                        for path in glob.glob(onedrive_business):
                            if os.path.exists(path):
                                possible_paths.append(path)
                        
                        # Check for folder redirects via registry (Windows only)
                        if sys.platform == 'win32':
                            try:
                                import winreg
                                key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
                                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
                                
                                shell_folder_names = {
                                    'Desktop': 'Desktop',
                                    'Documents': 'Personal',
                                    'Pictures': 'My Pictures',
                                    'Videos': 'My Video',
                                    'Music': 'My Music',
                                    'Downloads': '{374DE290-123F-4565-9164-39C4925E467B}'
                                }
                                
                                if folder_name in shell_folder_names:
                                    try:
                                        value, _ = winreg.QueryValueEx(key, shell_folder_names[folder_name])
                                        # Expand environment variables
                                        expanded = os.path.expandvars(value)
                                        if os.path.exists(expanded) and expanded not in possible_paths:
                                            possible_paths.append(expanded)
                                    except:
                                        pass
                                
                                winreg.CloseKey(key)
                            except:
                                pass
                        
                        return possible_paths
                    
                    folders_to_backup = {
                        'Desktop': 'Desktop',
                        'Documents': 'Documents', 
                        'Pictures': 'Pictures',
                        'Videos': 'Videos',
                        'Downloads': 'Downloads',
                        'Music': 'Music',
                        'Favorites': 'Favorites',
                        'Saved Games': 'Saved Games'
                    }
                    
                    total_folders = len(folders_to_backup)
                    completed = 0
                    successful_backups = 0
                    
                    for display_name, folder_name in folders_to_backup.items():
                        self.ui_queue.put(("log", f"Searching for {display_name}...", "INFO"))
                        
                        # Find all possible locations for this folder
                        found_paths = find_folder_path(folder_name)
                        
                        if not found_paths:
                            self.ui_queue.put(("log", f"⊘ {display_name} not found (checked standard & OneDrive locations)", "WARNING"))
                            completed += 1
                            self.ui_queue.put(("progress", completed / total_folders))
                            continue
                        
                        # Backup all found locations
                        for idx, source_folder in enumerate(found_paths):
                            location_suffix = ""
                            if len(found_paths) > 1:
                                if 'OneDrive' in source_folder:
                                    location_suffix = "_OneDrive"
                                else:
                                    location_suffix = f"_{idx+1}"
                            
                            dest_folder = os.path.join(backup_folder, display_name + location_suffix)
                            
                            self.ui_queue.put(("log", f"📂 Backing up {display_name} from: {source_folder}", "INFO"))
                            self.ui_queue.put(("progress", (completed + (idx / len(found_paths))) / total_folders))
                            
                            # Use robocopy for efficient backup with better error handling
                            cmd = [
                                'robocopy',
                                source_folder,
                                dest_folder,
                                '/E',      # Copy subdirectories including empty ones
                                '/COPYALL',# Copy all file info (timestamps, attributes, security, owner, auditing)
                                '/Z',      # Restartable mode
                                '/R:3',    # Retry 3 times (increased from 2)
                                '/W:10',   # Wait 10 seconds between retries (increased from 5)
                                '/MT:8',   # Multi-threaded
                                '/NP',     # No progress
                                '/NDL',    # No directory list
                                '/NFL',    # No file list
                                '/XJ',     # Exclude junction points (prevents loops)
                                '/XD', 'node_modules', '.git', '__pycache__'  # Exclude common junk folders
                            ]
                            
                            try:
                                process = subprocess.Popen(
                                    cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                                )
                                
                                stdout, stderr = process.communicate(timeout=1800)  # 30 minute timeout
                                returncode = process.returncode
                                
                                # Robocopy return codes:
                                # 0 = No files copied (already up to date)
                                # 1 = Files copied successfully
                                # 2 = Extra files or directories detected (success)
                                # 3 = Files copied + extras (success)
                                # 4-7 = Some files not copied but not critical
                                # 8+ = Serious errors
                                
                                if returncode < 4:
                                    self.ui_queue.put(("log", f"✓ {display_name} backed up successfully", "SUCCESS"))
                                    successful_backups += 1
                                elif returncode < 8:
                                    self.ui_queue.put(("log", f"⚠ {display_name} backed up with minor issues (some files skipped)", "WARNING"))
                                    successful_backups += 1
                                else:
                                    # Parse error details
                                    error_msg = f"✗ {display_name} backup failed (code {returncode})"
                                    if returncode == 9:
                                        error_msg += " - Permission denied on some files"
                                    elif returncode == 16:
                                        error_msg += " - Serious error occurred"
                                    
                                    self.ui_queue.put(("log", error_msg, "ERROR"))
                                    
                                    if stderr:
                                        self.ui_queue.put(("log", f"  Details: {stderr[:200]}", "ERROR"))
                                
                            except subprocess.TimeoutExpired:
                                process.kill()
                                self.ui_queue.put(("log", f"✗ {display_name} backup timed out (folder too large)", "ERROR"))
                            except Exception as e:
                                self.ui_queue.put(("log", f"✗ {display_name} backup error: {str(e)}", "ERROR"))
                        
                        completed += 1
                        self.ui_queue.put(("progress", completed / total_folders))
                    
                    self.ui_queue.put(("log", f"═══════════════════════════════", "INFO"))
                    self.ui_queue.put(("log", f"User Profile Backup Complete!", "SUCCESS"))
                    self.ui_queue.put(("log", f"Successfully backed up: {successful_backups}/{total_folders} folder groups", "SUCCESS"))
                    self.ui_queue.put(("log", f"Location: {backup_folder}", "SUCCESS"))
                    self.ui_queue.put(("progress", 1.0))
                    
                    # Send notification if enabled
                    self.send_notification(
                        "User Profile Backup Complete",
                        f"Backup completed for user: {username}\n"
                        f"Successfully backed up: {successful_backups}/{total_folders} folders\n"
                        f"Location: {backup_folder}"
                    )
                    
                    # Show completion message
                    def show_completion():
                        messagebox.showinfo(
                            "Backup Complete",
                            f"User profile backup completed!\n\n"
                            f"Successfully backed up: {successful_backups}/{total_folders} folder groups\n\n"
                            f"Backup saved to:\n{backup_folder}\n\n"
                            f"Would you like to open the backup folder?"
                        )
                        if messagebox.askyesno("Open Folder", "Open backup folder now?"):
                            if sys.platform == 'win32':
                                os.startfile(backup_folder)
                            elif sys.platform == 'darwin':
                                subprocess.Popen(['open', backup_folder])
                            else:
                                subprocess.Popen(['xdg-open', backup_folder])
                    
                    self.after(0, show_completion)
                    
                except Exception as e:
                    error_message = str(e)
                    self.ui_queue.put(("log", f"Backup error: {error_message}", "ERROR"))
                    logger.exception("User profile backup failed")

                    def show_error():
                        messagebox.showerror("Backup Error", f"An error occurred during backup:\n{error_message}")

                    self.after(0, show_error)
                
                finally:
                    def reset_ui():
                        self.set_operation_running(False)
                    
                    self.after(0, reset_ui)
            
            # Start backup thread
            self.current_operation_thread = threading.Thread(target=backup_worker, daemon=True)
            self.current_operation_thread.start()
            
        except Exception as e:
            logger.exception("Failed to start quick user backup")
            messagebox.showerror("Error", f"Failed to start backup:\n{str(e)}")
    
    
    
    def set_operation_running(self, running: bool):
        """Update UI state based on operation status."""
        self.operation_in_progress = running
        if running:
            self.cancel_btn.configure(state="normal")
            # Bounce continuously by default so the bar visibly moves even for operations
            # with no real percentage data - a real "progress" update (see process_ui_queue)
            # switches it to a determinate meter as soon as one comes in.
            self.progress_bar.configure(mode="indeterminate")
            self.progress_bar.start()
        else:
            self.cancel_btn.configure(state="disabled")
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self.progress_bar.set(0)
            self.status_label.configure(text="Ready")

    def check_not_busy(self) -> bool:
        """Guard against overlapping operations (e.g. imaging + service reset colliding
        and producing a real, not just stale, "another backup is in progress" error since
        nothing previously stopped two wbAdmin-touching actions from running at once).
        Returns True if it's OK to proceed, False (with a warning shown) if something
        is already running.
        """
        if self.operation_in_progress:
            messagebox.showwarning(
                "Operation In Progress",
                "Another operation is already running. Wait for it to finish before starting a new one."
            )
            return False
        return True

    def backup_browsers(self):
        """Backup browser data from Chrome, Firefox, and Edge."""
        if not self.check_not_busy():
            return
        try:
            # Ask for destination
            destination = filedialog.askdirectory(
                title="Select Destination for Browser Backup",
                initialdir=os.path.expanduser("~")
            )

            if not destination:
                return
            destination = os.path.normpath(destination)

            # Create timestamped backup folder
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_folder = os.path.join(destination, f"BrowserBackup_{timestamp}")
            
            try:
                os.makedirs(backup_folder, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Cannot create backup folder:\n{e}")
                return
            
            self.log_message("Starting browser data backup...", "INFO")
            self.set_operation_running(True)
            
            def browser_backup_worker():
                try:
                    username = os.getenv('USERNAME')
                    user_profile = os.path.join('C:\\Users', username)
                    browsers_found = 0
                    
                    # Chrome
                    chrome_paths = [
                        os.path.join(user_profile, 'AppData', 'Local', 'Google', 'Chrome', 'User Data'),
                        os.path.join(user_profile, 'AppData', 'Local', 'Chromium', 'User Data')
                    ]
                    
                    for chrome_path in chrome_paths:
                        if os.path.exists(chrome_path):
                            browser_name = "Chrome" if "Chrome" in chrome_path else "Chromium"
                            self.ui_queue.put(("log", f"Found {browser_name}, backing up...", "INFO"))
                            
                            dest = os.path.join(backup_folder, browser_name)
                            
                            # Copy important folders
                            important_items = ['Default', 'Profile 1', 'Profile 2', 'Bookmarks', 'Local State']
                            for item in important_items:
                                src_item = os.path.join(chrome_path, item)
                                if os.path.exists(src_item):
                                    dst_item = os.path.join(dest, item)
                                    try:
                                        if os.path.isdir(src_item):
                                            shutil.copytree(src_item, dst_item, ignore=shutil.ignore_patterns('Cache', 'Code Cache'))
                                        else:
                                            os.makedirs(dest, exist_ok=True)
                                            shutil.copy2(src_item, dst_item)
                                    except Exception as e:
                                        self.ui_queue.put(("log", f"  Warning: Could not copy {item}: {str(e)[:100]}", "WARNING"))
                            
                            self.ui_queue.put(("log", f"✓ {browser_name} backed up", "SUCCESS"))
                            browsers_found += 1
                    
                    # Firefox
                    firefox_path = os.path.join(user_profile, 'AppData', 'Roaming', 'Mozilla', 'Firefox', 'Profiles')
                    if os.path.exists(firefox_path):
                        self.ui_queue.put(("log", "Found Firefox, backing up...", "INFO"))
                        
                        dest = os.path.join(backup_folder, 'Firefox')
                        try:
                            shutil.copytree(firefox_path, dest, ignore=shutil.ignore_patterns('cache*', 'Cache'))
                            self.ui_queue.put(("log", "✓ Firefox backed up", "SUCCESS"))
                            browsers_found += 1
                        except Exception as e:
                            self.ui_queue.put(("log", f"✗ Firefox backup failed: {str(e)[:100]}", "ERROR"))
                    
                    # Edge
                    edge_path = os.path.join(user_profile, 'AppData', 'Local', 'Microsoft', 'Edge', 'User Data')
                    if os.path.exists(edge_path):
                        self.ui_queue.put(("log", "Found Edge, backing up...", "INFO"))
                        
                        dest = os.path.join(backup_folder, 'Edge')
                        
                        important_items = ['Default', 'Profile 1', 'Bookmarks', 'Local State']
                        for item in important_items:
                            src_item = os.path.join(edge_path, item)
                            if os.path.exists(src_item):
                                dst_item = os.path.join(dest, item)
                                try:
                                    if os.path.isdir(src_item):
                                        shutil.copytree(src_item, dst_item, ignore=shutil.ignore_patterns('Cache', 'Code Cache'))
                                    else:
                                        os.makedirs(dest, exist_ok=True)
                                        shutil.copy2(src_item, dst_item)
                                except Exception as e:
                                    self.ui_queue.put(("log", f"  Warning: Could not copy {item}: {str(e)[:100]}", "WARNING"))
                        
                        self.ui_queue.put(("log", "✓ Edge backed up", "SUCCESS"))
                        browsers_found += 1
                    
                    if browsers_found == 0:
                        self.ui_queue.put(("log", "No browsers found to backup", "WARNING"))
                    else:
                        self.ui_queue.put(("log", f"═══════════════════════════════", "INFO"))
                        self.ui_queue.put(("log", f"Browser Backup Complete! ({browsers_found} browsers)", "SUCCESS"))
                        self.ui_queue.put(("log", f"Location: {backup_folder}", "SUCCESS"))
                        
                        def show_completion():
                            messagebox.showinfo(
                                "Browser Backup Complete",
                                f"Backed up {browsers_found} browser(s) successfully!\n\n"
                                f"Location: {backup_folder}"
                            )
                            if messagebox.askyesno("Open Folder", "Open backup folder now?"):
                                os.startfile(backup_folder)
                        
                        self.after(0, show_completion)
                
                except Exception as e:
                    self.ui_queue.put(("log", f"Browser backup error: {str(e)}", "ERROR"))
                    logger.exception("Browser backup failed")
                
                finally:
                    def reset_ui():
                        self.set_operation_running(False)
                    self.after(0, reset_ui)
            
            threading.Thread(target=browser_backup_worker, daemon=True).start()
            
        except Exception as e:
            logger.exception("Failed to start browser backup")
            messagebox.showerror("Error", f"Failed to start browser backup:\n{str(e)}")
    
    def export_wifi_passwords(self):
        """Export all saved WiFi passwords to a text file."""
        if not self.check_not_busy():
            return
        try:
            # Ask for destination file
            file_path = filedialog.asksaveasfilename(
                title="Save WiFi Passwords As",
                defaultextension=".txt",
                filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
                initialfile=f"WiFi_Passwords_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            )

            if not file_path:
                return
            file_path = os.path.normpath(file_path)

            self.log_message("Exporting WiFi passwords...", "INFO")
            self.set_operation_running(True)
            
            def wifi_export_worker():
                try:
                    # Get list of WiFi profiles
                    profiles_result = subprocess.run(
                        ['netsh', 'wlan', 'show', 'profiles'],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    
                    if profiles_result.returncode != 0:
                        self.ui_queue.put(("log", "Failed to retrieve WiFi profiles", "ERROR"))
                        def show_error():
                            messagebox.showerror("Error", "Could not retrieve WiFi profiles. Make sure you're running as Administrator.")
                        self.after(0, show_error)
                        return
                    
                    # Parse profile names
                    profiles = []
                    for line in profiles_result.stdout.split('\n'):
                        if 'All User Profile' in line or 'Profil pour tous les utilisateurs' in line:
                            profile_name = line.split(':')[1].strip()
                            profiles.append(profile_name)
                    
                    self.ui_queue.put(("log", f"Found {len(profiles)} WiFi network(s)", "INFO"))
                    
                    # Export passwords
                    wifi_data = []
                    wifi_data.append("=" * 60)
                    wifi_data.append("WiFi Networks and Passwords")
                    wifi_data.append(f"Exported: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    wifi_data.append(f"Computer: {os.getenv('COMPUTERNAME', 'Unknown')}")
                    wifi_data.append("=" * 60)
                    wifi_data.append("")
                    
                    found_passwords = 0
                    
                    for profile in profiles:
                        self.ui_queue.put(("log", f"Processing: {profile}", "INFO"))
                        
                        # Get profile details
                        profile_result = subprocess.run(
                            ['netsh', 'wlan', 'show', 'profile', profile, 'key=clear'],
                            capture_output=True,
                            text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                        )
                        
                        if profile_result.returncode == 0:
                            password = "Not Found"
                            auth_type = "Unknown"
                            
                            for line in profile_result.stdout.split('\n'):
                                if 'Key Content' in line or 'Contenu de la clé' in line:
                                    password = line.split(':')[1].strip()
                                    found_passwords += 1
                                elif 'Authentication' in line or 'Authentification' in line:
                                    auth_type = line.split(':')[1].strip()
                            
                            wifi_data.append(f"Network Name: {profile}")
                            wifi_data.append(f"Authentication: {auth_type}")
                            wifi_data.append(f"Password: {password}")
                            wifi_data.append("-" * 60)
                            wifi_data.append("")
                    
                    # Write to file
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(wifi_data))
                    
                    self.ui_queue.put(("log", f"═══════════════════════════════", "INFO"))
                    self.ui_queue.put(("log", f"WiFi Export Complete!", "SUCCESS"))
                    self.ui_queue.put(("log", f"Networks: {len(profiles)}, Passwords: {found_passwords}", "SUCCESS"))
                    self.ui_queue.put(("log", f"File: {file_path}", "SUCCESS"))
                    
                    def show_completion():
                        messagebox.showinfo(
                            "WiFi Export Complete",
                            f"Exported {len(profiles)} network(s)\n"
                            f"Found passwords for {found_passwords} network(s)\n\n"
                            f"File saved to:\n{file_path}"
                        )
                        if messagebox.askyesno("Open File", "Open the WiFi passwords file now?"):
                            os.startfile(file_path)
                    
                    self.after(0, show_completion)
                
                except Exception as e:
                    error_message = str(e)
                    self.ui_queue.put(("log", f"WiFi export error: {error_message}", "ERROR"))
                    logger.exception("WiFi export failed")

                    def show_error():
                        messagebox.showerror("Export Error", f"Failed to export WiFi passwords:\n{error_message}")
                    self.after(0, show_error)
                
                finally:
                    def reset_ui():
                        self.set_operation_running(False)
                    self.after(0, reset_ui)
            
            threading.Thread(target=wifi_export_worker, daemon=True).start()
            
        except Exception as e:
            logger.exception("Failed to start WiFi export")
            messagebox.showerror("Error", f"Failed to start WiFi export:\n{str(e)}")

    def export_app_inventory(self):
        """Export installed apps (via winget) and generate a restore script for a clean-install migration."""
        if not self.check_not_busy():
            return
        try:
            destination = filedialog.askdirectory(
                title="Select Destination for App Inventory",
                initialdir=os.path.expanduser("~")
            )

            if not destination:
                return
            destination = os.path.normpath(destination)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_folder = os.path.join(destination, f"AppInventory_{timestamp}")

            try:
                os.makedirs(backup_folder, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Cannot create backup folder:\n{e}")
                return

            self.log_message("Starting app inventory export...", "INFO")
            self.set_operation_running(True)

            def app_inventory_worker():
                try:
                    if not shutil.which("winget"):
                        self.ui_queue.put(("log", "winget not found on this system", "ERROR"))

                        def show_missing():
                            messagebox.showerror(
                                "winget Not Found",
                                "winget (App Installer) was not found on this system.\n\n"
                                "Install 'App Installer' from the Microsoft Store, then try again."
                            )
                        self.after(0, show_missing)
                        return

                    apps_json = os.path.join(backup_folder, "apps.json")
                    apps_list_txt = os.path.join(backup_folder, "winget_list.txt")

                    self.ui_queue.put(("log", "Running winget export (this may take a minute)...", "INFO"))
                    export_result = subprocess.run(
                        ['winget', 'export', '-o', apps_json, '--include-versions',
                         '--accept-source-agreements'],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )

                    if export_result.returncode != 0 or not os.path.exists(apps_json):
                        self.ui_queue.put(("log", f"winget export failed: {export_result.stderr.strip()[:200]}", "ERROR"))
                        self.ui_queue.put(("log", "winget list output will still be saved as a fallback", "WARNING"))

                    # Human-readable list as a fallback / quick reference
                    list_result = subprocess.run(
                        ['winget', 'list'],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    with open(apps_list_txt, 'w', encoding='utf-8') as f:
                        f.write(f"Installed Applications - {os.getenv('COMPUTERNAME', 'Unknown')}\n")
                        f.write(f"Exported: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write("=" * 60 + "\n\n")
                        f.write(list_result.stdout)

                    app_count = 0
                    if os.path.exists(apps_json):
                        try:
                            with open(apps_json, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                            app_count = sum(len(s.get('Packages', [])) for s in data.get('Sources', []))
                        except Exception:
                            pass

                    # Restore script (PowerShell) - re-imports the winget package list
                    restore_ps1 = os.path.join(backup_folder, "restore_apps.ps1")
                    with open(restore_ps1, 'w', encoding='utf-8') as f:
                        f.write(
                            "# REGTeches App Restore Script\n"
                            f"# Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                            f"on {os.getenv('COMPUTERNAME', 'Unknown')}\n"
                            "# Run after a clean Windows install to reinstall your apps via winget.\n\n"
                            "if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {\n"
                            "    Write-Host \"winget not found. Install 'App Installer' from the Microsoft Store first.\" -ForegroundColor Red\n"
                            "    exit 1\n"
                            "}\n\n"
                            "Write-Host \"Restoring installed applications via winget...\" -ForegroundColor Cyan\n"
                            "winget import -i \"$PSScriptRoot\\apps.json\" --accept-package-agreements --accept-source-agreements\n"
                            "Write-Host \"Done. Review any packages above that failed to install.\" -ForegroundColor Green\n"
                        )

                    # .bat wrapper so it can be double-clicked from WinRE/USB without an execution-policy fight
                    restore_bat = os.path.join(backup_folder, "restore_apps.bat")
                    with open(restore_bat, 'w', encoding='utf-8') as f:
                        f.write(
                            "@echo off\r\n"
                            "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0restore_apps.ps1\"\r\n"
                            "pause\r\n"
                        )

                    self.ui_queue.put(("log", "═══════════════════════════════", "INFO"))
                    self.ui_queue.put(("log", f"App Inventory Export Complete! ({app_count} apps)", "SUCCESS"))
                    self.ui_queue.put(("log", f"Location: {backup_folder}", "SUCCESS"))

                    self.send_notification(
                        "App Inventory Export Complete",
                        f"Exported {app_count} app(s) to {backup_folder}"
                    )

                    def show_completion():
                        messagebox.showinfo(
                            "App Inventory Complete",
                            f"Exported {app_count} app(s) via winget.\n\n"
                            f"Location: {backup_folder}\n\n"
                            "Copy this folder to a USB drive. After a clean install, run "
                            "restore_apps.bat to reinstall everything."
                        )
                        if messagebox.askyesno("Open Folder", "Open the inventory folder now?"):
                            os.startfile(backup_folder)

                    self.after(0, show_completion)

                except Exception as e:
                    error_message = str(e)
                    self.ui_queue.put(("log", f"App inventory export error: {error_message}", "ERROR"))
                    logger.exception("App inventory export failed")

                    def show_error():
                        messagebox.showerror("Export Error", f"Failed to export app inventory:\n{error_message}")
                    self.after(0, show_error)

                finally:
                    def reset_ui():
                        self.set_operation_running(False)
                    self.after(0, reset_ui)

            threading.Thread(target=app_inventory_worker, daemon=True).start()

        except Exception as e:
            logger.exception("Failed to start app inventory export")
            messagebox.showerror("Error", f"Failed to start app inventory export:\n{str(e)}")

    def backup_environment_vars(self):
        """Backup user/system environment variables to .reg files plus a restore script."""
        if not self.check_not_busy():
            return
        try:
            destination = filedialog.askdirectory(
                title="Select Destination for Environment Backup",
                initialdir=os.path.expanduser("~")
            )

            if not destination:
                return
            destination = os.path.normpath(destination)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_folder = os.path.join(destination, f"EnvironmentBackup_{timestamp}")

            try:
                os.makedirs(backup_folder, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Cannot create backup folder:\n{e}")
                return

            self.log_message("Starting environment variable backup...", "INFO")
            self.set_operation_running(True)

            def env_backup_worker():
                try:
                    exported = []

                    # User environment variables (never needs admin)
                    hkcu_reg = os.path.join(backup_folder, "HKCU_Environment.reg")
                    self.ui_queue.put(("log", "Exporting user environment variables...", "INFO"))
                    result = subprocess.run(
                        ['reg', 'export', 'HKCU\\Environment', hkcu_reg, '/y'],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    if result.returncode == 0:
                        exported.append("HKCU_Environment.reg (user)")
                        self.ui_queue.put(("log", "✓ User environment variables exported", "SUCCESS"))
                    else:
                        self.ui_queue.put(("log", f"User env export failed: {result.stderr.strip()[:150]}", "WARNING"))

                    # System environment variables (requires Administrator)
                    hklm_reg = os.path.join(backup_folder, "HKLM_Environment.reg")
                    self.ui_queue.put(("log", "Exporting system environment variables...", "INFO"))
                    result = subprocess.run(
                        ['reg', 'export',
                         'HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment',
                         hklm_reg, '/y'],
                        capture_output=True,
                        text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    if result.returncode == 0:
                        exported.append("HKLM_Environment.reg (system, needs admin to restore)")
                        self.ui_queue.put(("log", "✓ System environment variables exported", "SUCCESS"))
                    else:
                        self.ui_queue.put(("log", "System env export skipped (run as Administrator to include it)", "WARNING"))

                    # Human-readable snapshot of the current process environment
                    env_txt = os.path.join(backup_folder, "environment_variables.txt")
                    with open(env_txt, 'w', encoding='utf-8') as f:
                        f.write(f"Environment Variables - {os.getenv('COMPUTERNAME', 'Unknown')}\n")
                        f.write(f"Exported: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write("=" * 60 + "\n\n")
                        for key in sorted(os.environ.keys()):
                            f.write(f"{key}={os.environ[key]}\n")

                    # Restore script
                    restore_ps1 = os.path.join(backup_folder, "restore_environment.ps1")
                    with open(restore_ps1, 'w', encoding='utf-8') as f:
                        f.write(
                            "# REGTeches Environment Restore Script\n"
                            f"# Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                            f"on {os.getenv('COMPUTERNAME', 'Unknown')}\n\n"
                            "$here = $PSScriptRoot\n"
                            "if (Test-Path \"$here\\HKCU_Environment.reg\") {\n"
                            "    Write-Host \"Restoring user environment variables...\" -ForegroundColor Cyan\n"
                            "    reg import \"$here\\HKCU_Environment.reg\"\n"
                            "}\n\n"
                            "if (Test-Path \"$here\\HKLM_Environment.reg\") {\n"
                            "    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)\n"
                            "    if ($isAdmin) {\n"
                            "        Write-Host \"Restoring system environment variables...\" -ForegroundColor Cyan\n"
                            "        reg import \"$here\\HKLM_Environment.reg\"\n"
                            "    } else {\n"
                            "        Write-Host \"Skipping system environment variables - re-run this script as Administrator to restore them.\" -ForegroundColor Yellow\n"
                            "    }\n"
                            "}\n\n"
                            "Write-Host \"Done. Sign out and back in for changes to fully apply.\" -ForegroundColor Green\n"
                        )

                    restore_bat = os.path.join(backup_folder, "restore_environment.bat")
                    with open(restore_bat, 'w', encoding='utf-8') as f:
                        f.write(
                            "@echo off\r\n"
                            "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0restore_environment.ps1\"\r\n"
                            "pause\r\n"
                        )

                    self.ui_queue.put(("log", "═══════════════════════════════", "INFO"))
                    self.ui_queue.put(("log", "Environment Backup Complete!", "SUCCESS"))
                    self.ui_queue.put(("log", f"Location: {backup_folder}", "SUCCESS"))

                    self.send_notification(
                        "Environment Backup Complete",
                        f"Exported: {', '.join(exported)}\nLocation: {backup_folder}"
                    )

                    def show_completion():
                        messagebox.showinfo(
                            "Environment Backup Complete",
                            "Exported: " + ", ".join(exported) + "\n\n"
                            f"Location: {backup_folder}\n\n"
                            "Run restore_environment.bat after a clean install to re-import these "
                            "settings (as Administrator, to include the system-level variables)."
                        )
                        if messagebox.askyesno("Open Folder", "Open the backup folder now?"):
                            os.startfile(backup_folder)

                    self.after(0, show_completion)

                except Exception as e:
                    error_message = str(e)
                    self.ui_queue.put(("log", f"Environment backup error: {error_message}", "ERROR"))
                    logger.exception("Environment backup failed")

                    def show_error():
                        messagebox.showerror("Backup Error", f"Failed to backup environment variables:\n{error_message}")
                    self.after(0, show_error)

                finally:
                    def reset_ui():
                        self.set_operation_running(False)
                    self.after(0, reset_ui)

            threading.Thread(target=env_backup_worker, daemon=True).start()

        except Exception as e:
            logger.exception("Failed to start environment backup")
            messagebox.showerror("Error", f"Failed to start environment backup:\n{str(e)}")

    def scan_backups(self):
        """Scan for available backups in the selected location."""
        backup_path = self.restore_backup_path.get().strip()
        
        if not backup_path or not os.path.exists(backup_path):
            messagebox.showwarning("Invalid Path", "Please select a valid backup location.")
            return
        
        self.log_message(f"Scanning for backups in: {backup_path}", "INFO")
        
        # Clear existing list
        for widget in self.restore_list_scroll.winfo_children():
            widget.destroy()
        
        try:
            # Find backup folders
            backups = []
            
            for item in os.listdir(backup_path):
                item_path = os.path.join(backup_path, item)
                if os.path.isdir(item_path):
                    # Check if it looks like a backup
                    if 'Backup' in item or 'backup' in item:
                        stat_info = os.stat(item_path)
                        backups.append({
                            'name': item,
                            'path': item_path,
                            'date': datetime.datetime.fromtimestamp(stat_info.st_mtime),
                            'size': sum(f.stat().st_size for f in Path(item_path).rglob('*') if f.is_file())
                        })
            
            if not backups:
                ctk.CTkLabel(
                    self.restore_list_scroll,
                    text="No backups found in this location",
                    font=("Arial", 11),
                    text_color="gray60"
                ).pack(pady=30)
                self.log_message("No backups found", "WARNING")
                return
            
            # Sort by date (newest first)
            backups.sort(key=lambda x: x['date'], reverse=True)
            
            self.log_message(f"Found {len(backups)} backup(s)", "SUCCESS")
            
            # Display backups
            for backup in backups:
                self.create_restore_widget(backup)
        
        except Exception as e:
            logger.exception("Failed to scan backups")
            messagebox.showerror("Scan Error", f"Failed to scan for backups:\n{str(e)}")
    
    def create_restore_widget(self, backup: Dict):
        """Create a widget for a single backup in the restore list."""
        frame = ctk.CTkFrame(self.restore_list_scroll)
        frame.pack(fill="x", pady=5, padx=5)
        
        info_frame = ctk.CTkFrame(frame, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        
        # Backup name
        name_label = ctk.CTkLabel(
            info_frame,
            text=backup['name'],
            font=("Arial", 12, "bold"),
            anchor="w"
        )
        name_label.pack(anchor="w")
        
        # Details
        size_mb = backup['size'] / (1024 * 1024)
        date_str = backup['date'].strftime("%Y-%m-%d %H:%M:%S")
        details = f"📅 {date_str}  •  💾 {size_mb:.1f} MB"
        
        details_label = ctk.CTkLabel(
            info_frame,
            text=details,
            font=("Arial", 9),
            text_color="gray60",
            anchor="w"
        )
        details_label.pack(anchor="w")
        
        # Buttons
        button_frame = ctk.CTkFrame(frame, fg_color="transparent")
        button_frame.pack(side="right", padx=10)
        
        ctk.CTkButton(
            button_frame,
            text="📂 Open Folder",
            width=120,
            command=lambda: os.startfile(backup['path'])
        ).pack(side="left", padx=2)
        
        ctk.CTkButton(
            button_frame,
            text="🔄 Restore",
            width=100,
            fg_color="#27AE60",
            hover_color="#229954",
            command=lambda: self.restore_backup(backup)
        ).pack(side="left", padx=2)
    
    def restore_backup(self, backup: Dict):
        """Restore files from a backup."""
        if not self.check_not_busy():
            return
        msg = (
            f"Restore from backup:\n\n"
            f"Backup: {backup['name']}\n"
            f"Date: {backup['date'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Size: {backup['size'] / (1024 * 1024):.1f} MB\n\n"
            f"Choose restore location:"
        )
        
        if not messagebox.askyesno("Restore Backup", msg):
            return
        
        # Ask for restore destination
        restore_dest = filedialog.askdirectory(
            title="Select Restore Destination",
            initialdir=os.path.expanduser("~")
        )

        if not restore_dest:
            return
        restore_dest = os.path.normpath(restore_dest)

        self.log_message(f"Restoring backup: {backup['name']}", "INFO")
        self.log_message(f"Destination: {restore_dest}", "INFO")
        self.set_operation_running(True)
        
        def restore_worker():
            try:
                # Use robocopy to restore
                cmd = [
                    'robocopy',
                    backup['path'],
                    restore_dest,
                    '/E',
                    '/Z',
                    '/R:3',
                    '/W:10',
                    '/MT:8',
                    '/NP'
                ]
                
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                
                process.wait()
                
                if process.returncode < 8:
                    self.ui_queue.put(("log", "Restore completed successfully!", "SUCCESS"))
                    
                    def show_completion():
                        messagebox.showinfo(
                            "Restore Complete",
                            f"Files restored to:\n{restore_dest}"
                        )
                        if messagebox.askyesno("Open Folder", "Open restored files now?"):
                            os.startfile(restore_dest)
                    
                    self.after(0, show_completion)
                else:
                    self.ui_queue.put(("log", f"Restore had errors (code {process.returncode})", "ERROR"))
                    
                    def show_error():
                        messagebox.showwarning("Restore Warning", "Restore completed with some errors. Check the log for details.")
                    
                    self.after(0, show_error)
            
            except Exception as e:
                self.ui_queue.put(("log", f"Restore error: {str(e)}", "ERROR"))
                logger.exception("Restore failed")
            
            finally:
                def reset_ui():
                    self.set_operation_running(False)
                self.after(0, reset_ui)
        
        threading.Thread(target=restore_worker, daemon=True).start()
    
    
    # ==================== PROFILE MANAGEMENT ====================
    
    def refresh_profiles_list(self):
        """Refresh the profiles display."""
        # Clear existing widgets
        for widget in self.profiles_scroll.winfo_children():
            widget.destroy()
        
        profiles = self.config.get("profiles", [])
        
        if not profiles:
            ctk.CTkLabel(
                self.profiles_scroll,
                text="No profiles yet. Create one from the Sync or Image tabs.",
                font=("Arial", 12),
                text_color="gray60"
            ).pack(pady=50)
            return
        
        for idx, profile in enumerate(profiles):
            self.create_profile_widget(profile, idx)
    
    def create_profile_widget(self, profile: Dict, index: int):
        """Create a widget for a single profile."""
        frame = ctk.CTkFrame(self.profiles_scroll)
        frame.pack(fill="x", pady=5, padx=5)
        
        # Profile info
        info_frame = ctk.CTkFrame(frame, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True, padx=15, pady=10)
        
        name_label = ctk.CTkLabel(
            info_frame,
            text=profile.get('name', 'Unnamed Profile'),
            font=("Arial", 13, "bold")
        )
        name_label.pack(anchor="w")
        
        type_label = ctk.CTkLabel(
            info_frame,
            text=f"Type: {profile.get('type', 'Unknown')}",
            font=("Arial", 10),
            text_color="gray70"
        )
        type_label.pack(anchor="w")
        
        if profile.get('type') == 'Sync':
            details = f"📂 {profile.get('src', 'N/A')} → 💾 {profile.get('dst', 'N/A')}"
        else:
            details = f"Drive: {profile.get('drive', 'N/A')} → {profile.get('dest', 'N/A')}"
        
        details_label = ctk.CTkLabel(
            info_frame,
            text=details,
            font=("Arial", 9),
            text_color="gray60"
        )
        details_label.pack(anchor="w")
        
        # Action buttons
        button_frame = ctk.CTkFrame(frame, fg_color="transparent")
        button_frame.pack(side="right", padx=10)
        
        ctk.CTkButton(
            button_frame,
            text="▶️ Run",
            width=80,
            fg_color="#27AE60",
            hover_color="#229954",
            command=lambda: self.run_profile(profile)
        ).pack(side="left", padx=2)
        
        ctk.CTkButton(
            button_frame,
            text="✏️ Edit",
            width=80,
            command=lambda: self.edit_profile(index)
        ).pack(side="left", padx=2)
        
        ctk.CTkButton(
            button_frame,
            text="🗑️ Delete",
            width=80,
            fg_color="#E74C3C",
            hover_color="#C0392B",
            command=lambda: self.delete_profile(index)
        ).pack(side="left", padx=2)
    
    def save_sync_profile(self):
        """Save current sync settings as a profile."""
        source = self.sync_src.get().strip()
        destination = self.sync_dst.get().strip()
        
        if not source or not destination:
            messagebox.showwarning("Incomplete Profile", "Please fill in both source and destination.")
            return
        
        # Get profile name
        dialog = ctk.CTkInputDialog(
            text="Enter a name for this profile:",
            title="Save Profile"
        )
        name = dialog.get_input()
        
        if name:
            profile = {
                "name": name,
                "type": "Sync",
                "src": source,
                "dst": destination,
                "created": datetime.datetime.now().isoformat()
            }
            
            self.config["profiles"].append(profile)
            self.save_config()
            self.refresh_profiles_list()
            
            messagebox.showinfo("Profile Saved", f"Profile '{name}' saved successfully!")
    
    def create_new_profile(self):
        """Create a new profile from scratch."""
        # Simple dialog to choose profile type
        dialog = ctk.CTkToplevel(self)
        dialog.title("New Profile")
        dialog.geometry("400x200")
        dialog.transient(self)
        dialog.grab_set()
        
        ctk.CTkLabel(
            dialog,
            text="Select Profile Type:",
            font=("Arial", 14, "bold")
        ).pack(pady=20)
        
        ctk.CTkButton(
            dialog,
            text="📁 Folder Sync Profile",
            command=lambda: [dialog.destroy(), self.tabs.set("Quick Sync")]
        ).pack(pady=5, padx=50, fill="x")
        
        ctk.CTkButton(
            dialog,
            text="💿 Disk Image Profile",
            command=lambda: [dialog.destroy(), self.tabs.set("Disk Image")]
        ).pack(pady=5, padx=50, fill="x")
    
    def run_profile(self, profile: Dict):
        """Execute a saved profile."""
        if not self.check_not_busy():
            return
        profile_type = profile.get('type')
        
        if profile_type == 'Sync':
            source = profile.get('src')
            destination = profile.get('dst')
            
            if not validate_path(source):
                messagebox.showerror("Invalid Profile", f"Source path no longer exists:\n{source}")
                return
            
            self.set_operation_running(True)
            self.current_operation_thread = threading.Thread(
                target=self._sync_worker,
                args=(source, destination, False),
                daemon=True
            )
            self.current_operation_thread.start()
        
        elif profile_type == 'Image':
            drive = profile.get('drive')
            destination = profile.get('dest')
            keep_count = profile.get('keep', 5)
            
            if not is_admin():
                messagebox.showerror("Admin Required", "Disk imaging requires administrator privileges.")
                return
            
            self.set_operation_running(True)
            self.current_operation_thread = threading.Thread(
                target=self._image_worker,
                args=(drive, destination, keep_count),
                daemon=True
            )
            self.current_operation_thread.start()
    
    def edit_profile(self, index: int):
        """Edit an existing profile."""
        if 0 <= index < len(self.config["profiles"]):
            profile = self.config["profiles"][index]
            
            # Create edit dialog
            dialog = ctk.CTkToplevel(self)
            dialog.title(f"Edit Profile: {profile.get('name')}")
            dialog.geometry("500x400")
            dialog.transient(self)
            dialog.grab_set()
            
            ctk.CTkLabel(
                dialog,
                text=f"Edit Profile: {profile.get('name')}",
                font=("Arial", 14, "bold")
            ).pack(pady=15)
            
            # Name field
            name_frame = ctk.CTkFrame(dialog, fg_color="transparent")
            name_frame.pack(fill="x", padx=30, pady=10)
            ctk.CTkLabel(name_frame, text="Profile Name:").pack(anchor="w")
            name_entry = ctk.CTkEntry(name_frame)
            name_entry.insert(0, profile.get('name', ''))
            name_entry.pack(fill="x")
            
            # Type-specific fields
            if profile.get('type') == 'Sync':
                src_frame = ctk.CTkFrame(dialog, fg_color="transparent")
                src_frame.pack(fill="x", padx=30, pady=5)
                ctk.CTkLabel(src_frame, text="Source:").pack(anchor="w")
                src_entry = ctk.CTkEntry(src_frame)
                src_entry.insert(0, profile.get('src', ''))
                src_entry.pack(fill="x")
                
                dst_frame = ctk.CTkFrame(dialog, fg_color="transparent")
                dst_frame.pack(fill="x", padx=30, pady=5)
                ctk.CTkLabel(dst_frame, text="Destination:").pack(anchor="w")
                dst_entry = ctk.CTkEntry(dst_frame)
                dst_entry.insert(0, profile.get('dst', ''))
                dst_entry.pack(fill="x")
            
            # Save button
            def save_changes():
                profile['name'] = name_entry.get()
                if profile.get('type') == 'Sync':
                    profile['src'] = src_entry.get()
                    profile['dst'] = dst_entry.get()
                
                self.save_config()
                self.refresh_profiles_list()
                dialog.destroy()
                messagebox.showinfo("Saved", "Profile updated successfully!")
            
            ctk.CTkButton(
                dialog,
                text="💾 Save Changes",
                command=save_changes
            ).pack(pady=20)
    
    def delete_profile(self, index: int):
        """Delete a profile."""
        if 0 <= index < len(self.config["profiles"]):
            profile = self.config["profiles"][index]
            
            if messagebox.askyesno(
                "Delete Profile",
                f"Are you sure you want to delete the profile '{profile.get('name')}'?"
            ):
                self.config["profiles"].pop(index)
                self.save_config()
                self.refresh_profiles_list()
                logger.info(f"Profile deleted: {profile.get('name')}")
    
    # ==================== SCHEDULE MANAGEMENT ====================
    
    def refresh_schedule_list(self):
        """Refresh the scheduled tasks display."""
        # Clear existing widgets
        for widget in self.schedule_scroll.winfo_children():
            widget.destroy()
        
        schedules = self.config.get("schedules", [])
        
        if not schedules:
            ctk.CTkLabel(
                self.schedule_scroll,
                text="No scheduled tasks. Click 'New Schedule' to create one.",
                font=("Arial", 12),
                text_color="gray60"
            ).pack(pady=50)
            return
        
        for idx, schedule in enumerate(schedules):
            self.create_schedule_widget(schedule, idx)
    
    def create_schedule_widget(self, schedule: Dict, index: int):
        """Create a widget for a single scheduled task."""
        frame = ctk.CTkFrame(self.schedule_scroll)
        frame.pack(fill="x", pady=5, padx=5)
        
        # Schedule info
        info_frame = ctk.CTkFrame(frame, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True, padx=15, pady=10)
        
        name_label = ctk.CTkLabel(
            info_frame,
            text=schedule.get('name', 'Unnamed Task'),
            font=("Arial", 13, "bold")
        )
        name_label.pack(anchor="w")
        
        profile_label = ctk.CTkLabel(
            info_frame,
            text=f"Profile: {schedule.get('profile_name', 'N/A')}",
            font=("Arial", 10),
            text_color="gray70"
        )
        profile_label.pack(anchor="w")
        
        freq_label = ctk.CTkLabel(
            info_frame,
            text=f"Frequency: {schedule.get('frequency', 'N/A')} at {schedule.get('time', 'N/A')}",
            font=("Arial", 9),
            text_color="gray60"
        )
        freq_label.pack(anchor="w")
        
        # Action buttons
        button_frame = ctk.CTkFrame(frame, fg_color="transparent")
        button_frame.pack(side="right", padx=10)
        
        ctk.CTkButton(
            button_frame,
            text="🗑️ Remove",
            width=100,
            fg_color="#E74C3C",
            hover_color="#C0392B",
            command=lambda: self.delete_schedule(index)
        ).pack(side="left", padx=2)
    
    def create_schedule(self):
        """Create a new scheduled task."""
        profiles = self.config.get("profiles", [])
        
        if not profiles:
            messagebox.showinfo(
                "No Profiles",
                "Please create at least one backup profile before scheduling."
            )
            return
        
        # Create schedule dialog
        dialog = ctk.CTkToplevel(self)
        dialog.title("New Scheduled Task")
        dialog.geometry("500x500")
        dialog.transient(self)
        dialog.grab_set()
        
        ctk.CTkLabel(
            dialog,
            text="Create Scheduled Backup",
            font=("Arial", 16, "bold")
        ).pack(pady=20)
        
        # Task name
        name_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        name_frame.pack(fill="x", padx=30, pady=10)
        ctk.CTkLabel(name_frame, text="Task Name:", font=("Arial", 12, "bold")).pack(anchor="w")
        name_entry = ctk.CTkEntry(name_frame, placeholder_text="e.g., Daily Documents Backup")
        name_entry.pack(fill="x", pady=5)
        
        # Profile selection
        profile_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        profile_frame.pack(fill="x", padx=30, pady=10)
        ctk.CTkLabel(profile_frame, text="Backup Profile:", font=("Arial", 12, "bold")).pack(anchor="w")
        profile_names = [p.get('name', 'Unnamed') for p in profiles]
        profile_combo = ctk.CTkComboBox(profile_frame, values=profile_names)
        profile_combo.pack(fill="x", pady=5)
        
        # Frequency
        freq_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        freq_frame.pack(fill="x", padx=30, pady=10)
        ctk.CTkLabel(freq_frame, text="Frequency:", font=("Arial", 12, "bold")).pack(anchor="w")
        freq_combo = ctk.CTkComboBox(
            freq_frame,
            values=["Daily", "Weekly", "Monthly"]
        )
        freq_combo.pack(fill="x", pady=5)
        
        # Time
        time_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        time_frame.pack(fill="x", padx=30, pady=10)
        ctk.CTkLabel(time_frame, text="Time:", font=("Arial", 12, "bold")).pack(anchor="w")
        time_entry = ctk.CTkEntry(time_frame, placeholder_text="HH:MM (24-hour format)")
        time_entry.insert(0, "02:00")
        time_entry.pack(fill="x", pady=5)
        
        # Create button
        def create_task():
            task_name = name_entry.get().strip()
            selected_profile = profile_combo.get()
            frequency = freq_combo.get()
            time = time_entry.get().strip()
            
            if not task_name or not selected_profile:
                messagebox.showwarning("Incomplete", "Please fill in all fields.")
                return
            
            # Validate time format
            try:
                datetime.datetime.strptime(time, "%H:%M")
            except ValueError:
                messagebox.showerror("Invalid Time", "Please use HH:MM format (e.g., 14:30)")
                return
            
            schedule = {
                "name": task_name,
                "profile_name": selected_profile,
                "frequency": frequency,
                "time": time,
                "created": datetime.datetime.now().isoformat()
            }
            
            if "schedules" not in self.config:
                self.config["schedules"] = []
            
            self.config["schedules"].append(schedule)
            self.save_config()
            self.refresh_schedule_list()
            
            # Create Windows Task Scheduler task
            self.create_windows_task(schedule)
            
            dialog.destroy()
            messagebox.showinfo("Success", f"Scheduled task '{task_name}' created!")
        
        ctk.CTkButton(
            dialog,
            text="✅ Create Task",
            font=("Arial", 13, "bold"),
            fg_color="#27AE60",
            hover_color="#229954",
            command=create_task
        ).pack(pady=30)
    
    def create_windows_task(self, schedule: Dict):
        """Create a task in Windows Task Scheduler."""
        try:
            task_name = f"BackupPro_{schedule['name'].replace(' ', '_')}"
            frequency = schedule['frequency']
            time = schedule['time']
            
            # Build schtasks command
            script_path = os.path.abspath(sys.argv[0])
            
            if frequency == "Daily":
                schedule_type = "DAILY"
            elif frequency == "Weekly":
                schedule_type = "WEEKLY"
            else:  # Monthly
                schedule_type = "MONTHLY"
            
            cmd = [
                'schtasks',
                '/Create',
                '/TN', task_name,
                '/TR', f'"{sys.executable}" "{script_path}"',
                '/SC', schedule_type,
                '/ST', time,
                '/F'  # Force create
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                logger.info(f"Windows task created: {task_name}")
            else:
                logger.error(f"Failed to create Windows task: {result.stderr}")
                messagebox.showwarning(
                    "Task Scheduler",
                    "Task saved in BackupPro but could not be added to Windows Task Scheduler.\n"
                    "You may need to create it manually."
                )
        except Exception as e:
            logger.error(f"Error creating Windows task: {e}")
    
    def delete_schedule(self, index: int):
        """Delete a scheduled task."""
        schedules = self.config.get("schedules", [])
        if 0 <= index < len(schedules):
            schedule = schedules[index]
            
            if messagebox.askyesno(
                "Delete Schedule",
                f"Are you sure you want to delete the scheduled task '{schedule.get('name')}'?"
            ):
                # Try to remove from Windows Task Scheduler
                task_name = f"BackupPro_{schedule['name'].replace(' ', '_')}"
                try:
                    subprocess.run(
                        ['schtasks', '/Delete', '/TN', task_name, '/F'],
                        capture_output=True
                    )
                except Exception as e:
                    logger.warning(f"Could not remove Windows task: {e}")
                
                schedules.pop(index)
                self.save_config()
                self.refresh_schedule_list()
                logger.info(f"Schedule deleted: {schedule.get('name')}")
    
    # ==================== SETTINGS ====================
    
    def show_settings(self):
        """Show settings dialog."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Settings")
        dialog.geometry("600x500")
        dialog.transient(self)
        dialog.grab_set()
        
        ctk.CTkLabel(
            dialog,
            text="⚙️ BackupPro Settings",
            font=("Arial", 18, "bold")
        ).pack(pady=20)
        
        # Settings frame
        settings_frame = ctk.CTkScrollableFrame(dialog)
        settings_frame.pack(fill="both", expand=True, padx=20, pady=10)
        
        settings = self.config.get("settings", DEFAULT_SETTINGS)
        
        # NAS IP
        nas_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        nas_frame.pack(fill="x", pady=10)
        ctk.CTkLabel(nas_frame, text="NAS / Server IP Address:", font=("Arial", 12, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            nas_frame,
            text="The device the dashboard pings to show online/offline status. Leave blank to disable.",
            font=("Arial", 9),
            text_color="gray60"
        ).pack(anchor="w")

        nas_entry_row = ctk.CTkFrame(nas_frame, fg_color="transparent")
        nas_entry_row.pack(fill="x", pady=5)

        nas_entry = ctk.CTkEntry(nas_entry_row, placeholder_text="e.g. 192.168.1.100")
        nas_entry.insert(0, settings.get('nas_ip', ''))
        nas_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        nas_test_result = ctk.CTkLabel(nas_frame, text="", font=("Arial", 10))
        nas_test_result.pack(anchor="w")

        def test_nas_connection():
            ip = nas_entry.get().strip()
            if not ip:
                nas_test_result.configure(text="Enter an IP address to test", text_color="gray60")
                return
            nas_test_result.configure(text=f"Pinging {ip}...", text_color="gray60")

            def do_test():
                online = self.engine.check_nas_status(ip)
                def show_result():
                    if online:
                        nas_test_result.configure(text=f"✓ {ip} is reachable", text_color="#27AE60")
                    else:
                        nas_test_result.configure(text=f"✗ {ip} did not respond", text_color="#E74C3C")
                self.after(0, show_result)

            threading.Thread(target=do_test, daemon=True).start()

        ctk.CTkButton(
            nas_entry_row,
            text="🔌 Test",
            width=80,
            command=test_nas_connection
        ).pack(side="right")

        # NAS Login (for network backup targets)
        ctk.CTkLabel(
            settings_frame,
            text="🔐 NAS Login (for network image backups)",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", pady=(15, 5))

        ctk.CTkLabel(
            settings_frame,
            text="Saved here so you're not asked every time you back up to a network share. "
                 "wbAdmin needs this explicitly — elevated processes can't see your normal "
                 "mapped-drive login.",
            font=("Arial", 9),
            text_color="gray60",
            wraplength=520,
            justify="left"
        ).pack(anchor="w", pady=(0, 5))

        nas_user_entry = ctk.CTkEntry(settings_frame, placeholder_text="NAS username")
        nas_user_entry.insert(0, settings.get('nas_username', ''))
        nas_user_entry.pack(fill="x", pady=(0, 5))

        nas_pass_entry = ctk.CTkEntry(settings_frame, show="*", placeholder_text="NAS password")
        nas_pass_entry.insert(0, settings.get('nas_password', ''))
        nas_pass_entry.pack(fill="x", pady=(0, 15))

        # Email settings
        ctk.CTkLabel(
            settings_frame,
            text="📧 Email Notifications",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", pady=(15, 5))
        
        email_enabled = ctk.CTkCheckBox(
            settings_frame,
            text="Enable email notifications"
        )
        if settings.get('notification_enabled'):
            email_enabled.select()
        email_enabled.pack(anchor="w", pady=5)
        
        email_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        email_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(email_frame, text="Sender Email:", font=("Arial", 11)).pack(anchor="w")
        email_entry = ctk.CTkEntry(email_frame)
        email_entry.insert(0, settings.get('sender_email', ''))
        email_entry.pack(fill="x", pady=5)
        
        ctk.CTkLabel(email_frame, text="App Password:", font=("Arial", 11)).pack(anchor="w")
        password_entry = ctk.CTkEntry(email_frame, show="*")
        password_entry.insert(0, settings.get('email_password', ''))
        password_entry.pack(fill="x", pady=5)
        
        ctk.CTkLabel(
            email_frame,
            text="ℹ️ Use Gmail App Password, not your regular password",
            font=("Arial", 9),
            text_color="gray60"
        ).pack(anchor="w")

        # SMS Text Alerts
        ctk.CTkLabel(
            settings_frame,
            text="📱 SMS Text Alerts",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", pady=(15, 5))

        ctk.CTkLabel(
            settings_frame,
            text="Texts your phone via your carrier's email-to-SMS gateway, using the same Gmail "
                 "account above — no extra service or API key needed.",
            font=("Arial", 9),
            text_color="gray60",
            wraplength=520,
            justify="left"
        ).pack(anchor="w", pady=(0, 5))

        sms_enabled_box = ctk.CTkCheckBox(settings_frame, text="Enable SMS text alerts")
        if settings.get('sms_enabled'):
            sms_enabled_box.select()
        sms_enabled_box.pack(anchor="w", pady=5)

        sms_recipients = [r.copy() for r in settings.get('sms_recipients', [])]

        sms_list_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        sms_list_frame.pack(fill="x", pady=5)

        def refresh_sms_list():
            for widget in sms_list_frame.winfo_children():
                widget.destroy()

            if not sms_recipients:
                ctk.CTkLabel(
                    sms_list_frame, text="No phone numbers added yet",
                    font=("Arial", 10), text_color="gray60"
                ).pack(anchor="w", pady=3)
                return

            for idx, recip in enumerate(sms_recipients):
                row = ctk.CTkFrame(sms_list_frame, fg_color="transparent")
                row.pack(fill="x", pady=2)
                ctk.CTkLabel(
                    row,
                    text=f"📱 {recip.get('label', 'Phone')} — {recip.get('phone', '')} ({recip.get('carrier', '')})",
                    font=("Arial", 10),
                    anchor="w"
                ).pack(side="left", fill="x", expand=True)
                ctk.CTkButton(
                    row, text="✕", width=28, height=24,
                    fg_color="#E74C3C", hover_color="#C0392B",
                    command=lambda i=idx: remove_sms_recipient(i)
                ).pack(side="right")

        def remove_sms_recipient(idx):
            if 0 <= idx < len(sms_recipients):
                sms_recipients.pop(idx)
            refresh_sms_list()

        def add_sms_recipient():
            add_dlg = ctk.CTkToplevel(dialog)
            add_dlg.title("Add Phone Number")
            add_dlg.geometry("460x540")
            add_dlg.transient(dialog)
            add_dlg.grab_set()

            ctk.CTkLabel(add_dlg, text="📱 Add Phone Number", font=("Arial", 14, "bold")).pack(pady=(15, 10))

            form = ctk.CTkFrame(add_dlg, fg_color="transparent")
            form.pack(fill="x", padx=20)

            ctk.CTkLabel(form, text="Label (name):", font=("Arial", 11)).pack(anchor="w", pady=(5, 0))
            label_entry = ctk.CTkEntry(form)
            label_entry.pack(fill="x", pady=(0, 8))

            ctk.CTkLabel(form, text="Phone number:", font=("Arial", 11)).pack(anchor="w")
            phone_entry = ctk.CTkEntry(form, placeholder_text="5551234567")
            phone_entry.pack(fill="x", pady=(0, 8))

            ctk.CTkLabel(form, text="Carrier:", font=("Arial", 11)).pack(anchor="w")
            carrier_combo = ctk.CTkComboBox(form, values=sorted(SMS_CARRIERS.keys()))
            carrier_combo.set("Verizon")
            carrier_combo.pack(fill="x", pady=(0, 8))

            status_lbl = ctk.CTkLabel(add_dlg, text="", font=("Arial", 10), text_color="#E74C3C")
            status_lbl.pack(pady=5)

            def do_add():
                phone = phone_entry.get().strip().replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
                carrier = carrier_combo.get()
                label = label_entry.get().strip() or "Phone"
                if not phone or not phone.isdigit() or len(phone) < 10:
                    status_lbl.configure(text="Enter a valid 10-digit phone number")
                    return
                if carrier not in SMS_CARRIERS:
                    status_lbl.configure(text="Select a valid carrier")
                    return
                sms_recipients.append({"phone": phone, "carrier": carrier, "label": label})
                refresh_sms_list()
                add_dlg.destroy()

            btn_row = ctk.CTkFrame(add_dlg, fg_color="transparent")
            btn_row.pack(pady=15)
            ctk.CTkButton(btn_row, text="Add", command=do_add).pack(side="left", padx=5)
            ctk.CTkButton(btn_row, text="Cancel", command=add_dlg.destroy).pack(side="left", padx=5)

            label_entry.focus_set()

        refresh_sms_list()

        ctk.CTkButton(
            settings_frame,
            text="➕ Add Phone Number",
            fg_color="#3498DB",
            hover_color="#2980B9",
            command=add_sms_recipient
        ).pack(fill="x", pady=(0, 10))

        # ntfy Push Notifications
        ctk.CTkLabel(
            settings_frame,
            text="🔔 ntfy Push Notifications",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", pady=(15, 5))

        ctk.CTkLabel(
            settings_frame,
            text="Free instant push notifications to your phone or desktop — install the ntfy app "
                 "(ntfy.sh) and subscribe to the topic name below. No account, no Gmail needed for this one.",
            font=("Arial", 9),
            text_color="gray60",
            wraplength=520,
            justify="left"
        ).pack(anchor="w", pady=(0, 5))

        ntfy_enabled_box = ctk.CTkCheckBox(settings_frame, text="Enable ntfy push notifications")
        if settings.get('ntfy_enabled'):
            ntfy_enabled_box.select()
        ntfy_enabled_box.pack(anchor="w", pady=5)

        ntfy_topic_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        ntfy_topic_row.pack(fill="x", pady=5)

        ntfy_topic_entry = ctk.CTkEntry(
            ntfy_topic_row, placeholder_text="e.g. regteches-backuppro-4f9a2c"
        )
        ntfy_topic_entry.insert(0, settings.get('ntfy_topic', ''))
        ntfy_topic_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        def generate_ntfy_topic():
            ntfy_topic_entry.delete(0, tk.END)
            ntfy_topic_entry.insert(0, f"regteches-backuppro-{secrets.token_hex(4)}")

        ctk.CTkButton(
            ntfy_topic_row,
            text="🎲 Generate",
            width=90,
            command=generate_ntfy_topic
        ).pack(side="right")

        ctk.CTkLabel(
            settings_frame,
            text="⚠️ Anyone who knows this topic name can read your notifications on public ntfy.sh "
                 "— keep it random, don't use something guessable like 'backup'.",
            font=("Arial", 9),
            text_color="#E67E22",
            wraplength=520,
            justify="left"
        ).pack(anchor="w", pady=(0, 5))

        ntfy_server_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        ntfy_server_row.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(ntfy_server_row, text="Server:", font=("Arial", 10)).pack(side="left", padx=(0, 5))
        ntfy_server_entry = ctk.CTkEntry(ntfy_server_row, placeholder_text="https://ntfy.sh")
        ntfy_server_entry.insert(0, settings.get('ntfy_server', 'https://ntfy.sh'))
        ntfy_server_entry.pack(side="left", fill="x", expand=True)

        # Test button (shared by email + SMS + ntfy, uses whatever is currently in the form)
        notify_test_result = ctk.CTkLabel(
            settings_frame, text="", font=("Arial", 10), wraplength=520, justify="left"
        )

        def send_test_notification():
            test_email_on = bool(email_enabled.get())
            test_sms_on = bool(sms_enabled_box.get()) and bool(sms_recipients)
            test_ntfy_on = bool(ntfy_enabled_box.get()) and bool(ntfy_topic_entry.get().strip())
            sender = email_entry.get().strip()
            pw = password_entry.get().strip()
            ntfy_server = ntfy_server_entry.get().strip() or "https://ntfy.sh"
            ntfy_topic = ntfy_topic_entry.get().strip()

            if not test_email_on and not test_sms_on and not test_ntfy_on:
                notify_test_result.configure(text="Enable email, SMS, and/or ntfy alerts first", text_color="gray60")
                return
            if (test_email_on or test_sms_on) and (not sender or not pw):
                notify_test_result.configure(
                    text="Enter your Gmail address and App Password first", text_color="#E67E22"
                )
                return

            notify_test_result.configure(text="📤 Sending test...", text_color="gray60")

            def do_test():
                results = self._deliver_alert(
                    sender, pw, "smtp.gmail.com", 465,
                    test_email_on, test_sms_on, sms_recipients,
                    test_ntfy_on, ntfy_server, ntfy_topic,
                    "Test Alert", f"[BackupPro] Test alert sent {datetime.datetime.now().strftime('%I:%M %p')}"
                )

                def show_result():
                    parts = []
                    if results["email_sent"]:
                        parts.append("✅ Email sent")
                    if results["sms_sent"]:
                        parts.append(f"✅ Texted {len(results['sms_sent'])} recipient(s)")
                    if results["ntfy_sent"]:
                        parts.append("✅ ntfy sent")
                    if results["errors"]:
                        parts.append(f"❌ {len(results['errors'])} error(s): {'; '.join(results['errors'])}")

                    any_sent = results["email_sent"] or results["sms_sent"] or results["ntfy_sent"]
                    if results["errors"] and not any_sent:
                        notify_test_result.configure(text=" / ".join(parts), text_color="#E74C3C")
                    elif results["errors"]:
                        notify_test_result.configure(text=" / ".join(parts), text_color="#E67E22")
                    else:
                        notify_test_result.configure(text=" / ".join(parts), text_color="#27AE60")

                self.after(0, show_result)

            threading.Thread(target=do_test, daemon=True).start()

        ctk.CTkButton(
            settings_frame,
            text="📤 Send Test Notification",
            font=("Arial", 12, "bold"),
            fg_color="#2C3E50",
            hover_color="#1B2631",
            command=send_test_notification
        ).pack(fill="x", pady=(0, 5))

        notify_test_result.pack(anchor="w", pady=(0, 15))

        # Retention
        retention_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        retention_frame.pack(fill="x", pady=15)
        ctk.CTkLabel(
            retention_frame,
            text="Default Image Retention:",
            font=("Arial", 12, "bold")
        ).pack(anchor="w")
        retention_slider = ctk.CTkSlider(retention_frame, from_=1, to=20, number_of_steps=19)
        retention_slider.set(settings.get('keep_limit', 5))
        retention_slider.pack(fill="x", pady=5)
        retention_label = ctk.CTkLabel(retention_frame, text=f"{int(retention_slider.get())} backups")
        retention_label.pack(anchor="w")
        
        def update_retention_label(value):
            retention_label.configure(text=f"{int(float(value))} backups")
        
        retention_slider.configure(command=update_retention_label)
        
        # Save button
        def save_settings():
            self.config["settings"]["nas_ip"] = nas_entry.get()
            self.config["settings"]["nas_username"] = nas_user_entry.get().strip()
            self.config["settings"]["nas_password"] = nas_pass_entry.get()
            self.config["settings"]["notification_enabled"] = email_enabled.get()
            self.config["settings"]["sender_email"] = email_entry.get()
            self.config["settings"]["email_password"] = password_entry.get()
            self.config["settings"]["sms_enabled"] = sms_enabled_box.get()
            self.config["settings"]["sms_recipients"] = sms_recipients
            self.config["settings"]["ntfy_enabled"] = ntfy_enabled_box.get()
            self.config["settings"]["ntfy_topic"] = ntfy_topic_entry.get().strip()
            self.config["settings"]["ntfy_server"] = ntfy_server_entry.get().strip() or "https://ntfy.sh"
            self.config["settings"]["keep_limit"] = int(retention_slider.get())

            self.save_config()
            dialog.destroy()
            messagebox.showinfo("Settings", "Settings saved successfully!")
            self.update_nas_status()
        
        button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        button_frame.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkButton(
            button_frame,
            text="💾 Save Settings",
            font=("Arial", 13, "bold"),
            command=save_settings
        ).pack(side="left", padx=5, expand=True, fill="x")
        
        ctk.CTkButton(
            button_frame,
            text="Cancel",
            command=dialog.destroy
        ).pack(side="right", padx=5, expand=True, fill="x")
    
    # ==================== HELP ====================
    
    def show_help(self):
        """Show help dialog."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Help - BackupPro v3.0")
        dialog.geometry("700x600")
        dialog.transient(self)
        
        help_text = ctk.CTkTextbox(dialog, font=("Consolas", 11), wrap="word")
        help_text.pack(fill="both", expand=True, padx=20, pady=20)
        
        help_content = """
BackupPro v3.0 - Professional Backup Suite
==========================================

QUICK START
-----------
1. Configure your backup paths in the Quick Sync or Disk Image tabs
2. Test with a dry run (Sync tab only)
3. Save frequently-used configurations as Profiles
4. Schedule automatic backups in the Schedule tab

FEATURES
--------
📁 Quick Sync
  - Mirror folders using robocopy
  - Fast, efficient file-level backup
  - Preview changes with dry-run mode
  - ⚠️ Warning: Destination will match source exactly (files deleted if not in source)

💿 Disk Image
  - Full system drive imaging using Windows Backup
  - Requires Administrator privileges
  - Automatic retention management (keeps last N backups)
  - Suitable for disaster recovery

📋 Profiles
  - Save backup configurations for repeated use
  - Quick execution of common backup tasks
  - Edit or delete profiles as needed

⏰ Schedule
  - Automate backups via Windows Task Scheduler
  - Daily, weekly, or monthly schedules
  - Runs even when BackupPro is closed

TIPS
----
• Run BackupPro as Administrator for full functionality
• Test new profiles with dry-run mode first
• Monitor the Activity Log for operation status
• Configure email notifications in Settings for alerts
• Verify NAS connectivity before starting backups
• Keep at least 3-5 system images for safety

KEYBOARD SHORTCUTS
------------------
Ctrl+S  - Quick save profile
Ctrl+Q  - Quit application
F5      - Refresh all lists

TROUBLESHOOTING
---------------
• "Administrator Required" - Right-click and "Run as Administrator"
• "Invalid Path" - Check folder exists and you have permissions
• "NAS Offline" - Verify network connection and NAS IP in Settings
• Email not working - Use Gmail App Password, not regular password
• Operation stuck - Use Cancel button or restart application

SUPPORT
-------
For issues or questions:
• Check the Activity Log for detailed error messages
• Review backup_log.txt in the application directory
• Ensure all paths are valid and accessible
• Verify you have sufficient disk space

VERSION HISTORY
---------------
v3.0 - Complete rewrite with security fixes and full functionality
v2.2 - Added profiles and scheduling (deprecated)
v2.0 - Initial public release

© 2026 BackupPro - Professional Data Protection
"""
        
        help_text.insert("1.0", help_content)
        help_text.configure(state="disabled")
        
        ctk.CTkButton(
            dialog,
            text="Close",
            command=dialog.destroy
        ).pack(pady=10)
    
    # ==================== UTILITY METHODS ====================
    
    def update_nas_status(self):
        """Check and update NAS status."""
        nas_ip = self.config.get("settings", {}).get("nas_ip", "")
        
        if not nas_ip:
            self.nas_status_label.configure(text="NAS: Not Configured", text_color="gray")
            return
        
        # Run check in thread to avoid UI freeze
        def check():
            online = self.engine.check_nas_status(nas_ip)
            
            def update_ui():
                if online:
                    self.nas_status_label.configure(
                        text=f"NAS: Online ({nas_ip})",
                        text_color="#27AE60"
                    )
                    self.log_message(f"NAS {nas_ip} is online", "SUCCESS")
                else:
                    self.nas_status_label.configure(
                        text=f"NAS: Offline ({nas_ip})",
                        text_color="#E74C3C"
                    )
                    self.log_message(f"NAS {nas_ip} is offline", "WARNING")
            
            self.after(0, update_ui)
        
        threading.Thread(target=check, daemon=True).start()
    
    def handle_engine_update(self, message: str, progress: Optional[float] = None, level: str = "INFO"):
        """Handle updates from the backup engine."""
        self.ui_queue.put(("log", message, level))
        if progress is not None:
            self.ui_queue.put(("progress", progress))
        if message:
            self.ui_queue.put(("status", message))
    
    def process_ui_queue(self):
        """Process queued UI updates from worker threads."""
        try:
            while True:
                item = self.ui_queue.get_nowait()
                
                if item[0] == "log":
                    self.log_message(item[1], item[2])
                    # Mirror onto the big status label too, not just the small event log,
                    # so it's obvious something is actively happening - skip cosmetic
                    # separator lines and keep it to a single readable line.
                    first_line = item[1].split('\n', 1)[0].strip()
                    if first_line and not first_line.startswith('═'):
                        self.status_label.configure(text=first_line[:120])
                elif item[0] == "progress":
                    # A real percentage arrived - switch off the indeterminate bounce and
                    # show the actual value instead.
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate")
                    self.progress_bar.set(item[1])
                elif item[0] == "status":
                    self.status_label.configure(text=item[1])
        except queue.Empty:
            pass
        
        # Schedule next check
        self.after(100, self.process_ui_queue)
    
    def log_message(self, message: str, level: str = "INFO"):
        """Add a message to the log display."""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        
        # Color coding
        colors = {
            "INFO": "#3498DB",
            "SUCCESS": "#27AE60",
            "WARNING": "#F39C12",
            "ERROR": "#E74C3C"
        }
        color = colors.get(level, "white")
        
        formatted = f"[{timestamp}] {level}: {message}\n"
        
        self.log_text.configure(state="normal")
        self.log_text.insert("end", formatted)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        
        # Also log to file
        logger.log(getattr(logging, level, logging.INFO), message)
    
    def clear_log(self):
        """Clear the log display."""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
    
    def _send_ntfy(self, server: str, topic: str, title: str, body: str) -> Optional[str]:
        """POST a push notification to an ntfy.sh (or self-hosted) topic.

        Title/priority go in the URL query string (not headers) since ntfy headers must be
        ASCII-safe and Python's http layer rejects unicode there - this sidesteps that
        entirely, matching the approach already used in the weather project's GitHub Action.
        Returns None on success, or an error string on failure.
        """
        try:
            base = (server or "https://ntfy.sh").rstrip('/')
            query = urllib.parse.urlencode({"title": f"BackupPro: {title}", "priority": "default"})
            url = f"{base}/{urllib.parse.quote(topic)}?{query}"
            req = urllib.request.Request(url, data=body.encode('utf-8'), method='POST')
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    return f"ntfy returned HTTP {resp.status}"
            return None
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    def _deliver_alert(self, sender_email: str, password: str, smtp_server: str, smtp_port,
                        send_email: bool, send_sms: bool, sms_recipients: List[Dict],
                        send_ntfy: bool, ntfy_server: str, ntfy_topic: str,
                        subject: str, body: str) -> Dict[str, Any]:
        """Send an email, SMS-via-carrier-gateway, and/or ntfy push alert.

        Shared by send_notification() (live alerts) and the Settings 'Send Test' button,
        so both use identical delivery logic. Returns what was sent / what failed instead
        of raising, so callers can report partial success (e.g. email OK, ntfy unreachable).
        ntfy uses a separate HTTP request, not SMTP, so it's tried independently and doesn't
        get blocked by (or block) an email/SMS login failure.
        """
        results = {"email_sent": False, "sms_sent": [], "ntfy_sent": False, "errors": []}

        if send_email or send_sms:
            try:
                port = int(smtp_port or 465)
                server = smtp_server or "smtp.gmail.com"

                if port == 465:
                    smtp = smtplib.SMTP_SSL(server, port, timeout=15)
                else:
                    smtp = smtplib.SMTP(server, port, timeout=15)
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()

                with smtp:
                    smtp.login(sender_email, password)

                    if send_email:
                        msg = EmailMessage()
                        msg.set_content(body)
                        msg['Subject'] = f"BackupPro: {subject}"
                        msg['From'] = sender_email
                        msg['To'] = sender_email
                        smtp.send_message(msg)
                        results["email_sent"] = True

                    if send_sms:
                        for recip in sms_recipients:
                            phone = recip.get('phone', '').strip().replace('-', '').replace(' ', '')
                            carrier = recip.get('carrier', '')
                            gateway = SMS_CARRIERS.get(carrier)
                            label = recip.get('label', phone)
                            if not phone or not gateway:
                                results["errors"].append(f"No gateway for '{carrier}' — skipped {label}")
                                continue
                            sms_msg = EmailMessage()
                            sms_msg.set_content(body)
                            sms_msg['Subject'] = subject
                            sms_msg['From'] = sender_email
                            sms_msg['To'] = f"{phone}@{gateway}"
                            smtp.send_message(sms_msg)
                            results["sms_sent"].append(f"{label} ({phone}@{gateway})")

            except smtplib.SMTPAuthenticationError as e:
                detail = e.smtp_error.decode() if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
                results["errors"].append(f"SMTP login failed: {detail}")
            except Exception as e:
                results["errors"].append(f"{type(e).__name__}: {e}")

        if send_ntfy:
            if not ntfy_topic:
                results["errors"].append("ntfy topic not set")
            else:
                error = self._send_ntfy(ntfy_server, ntfy_topic, subject, body)
                if error:
                    results["errors"].append(f"ntfy: {error}")
                else:
                    results["ntfy_sent"] = True

        return results

    def send_notification(self, subject: str, body: str):
        """Send an email, SMS text, and/or ntfy push alert if enabled in Settings."""
        settings = self.config.get("settings", {})

        email_on = bool(settings.get('notification_enabled'))
        sms_recipients = settings.get('sms_recipients', [])
        sms_on = bool(settings.get('sms_enabled')) and bool(sms_recipients)
        ntfy_on = bool(settings.get('ntfy_enabled')) and bool(settings.get('ntfy_topic'))

        if not email_on and not sms_on and not ntfy_on:
            return

        sender_email = settings.get('sender_email', '')
        password = settings.get('email_password', '')

        if (email_on or sms_on) and (not sender_email or not password):
            logger.warning("Email/SMS notification skipped: email credentials not configured")
            email_on = sms_on = False
            if not ntfy_on:
                return

        def send():
            results = self._deliver_alert(
                sender_email, password,
                settings.get('smtp_server'), settings.get('smtp_port'),
                email_on, sms_on, sms_recipients,
                ntfy_on, settings.get('ntfy_server'), settings.get('ntfy_topic'),
                subject, body
            )
            if results["email_sent"]:
                logger.info(f"Email notification sent: {subject}")
            if results["sms_sent"]:
                logger.info(f"SMS notification sent to {len(results['sms_sent'])} recipient(s): {subject}")
            if results["ntfy_sent"]:
                logger.info(f"ntfy notification sent: {subject}")
            for err in results["errors"]:
                logger.error(f"Notification error: {err}")

        threading.Thread(target=send, daemon=True).start()

    def on_closing(self):
        """Handle application close."""
        if self.engine.is_running:
            if not messagebox.askyesno(
                "Operation Running",
                "A backup operation is currently running. Are you sure you want to quit?"
            ):
                return
        
        logger.info("BackupPro closing")
        self.destroy()


def main():
    """Main entry point."""
    # Check for admin and offer to restart if needed
    if not is_admin():
        msg = (
            "BackupPro is not running with Administrator privileges.\n\n"
            "Some features (disk imaging) require admin rights.\n\n"
            "Restart as Administrator?"
        )
        
        root = tk.Tk()
        root.withdraw()
        
        if messagebox.askyesno("Administrator Required", msg):
            try:
                ctypes.windll.shell32.ShellExecuteW(
                    None,
                    "runas",
                    sys.executable,
                    " ".join(sys.argv),
                    None,
                    1
                )
                sys.exit(0)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to restart as admin: {e}")
        
        root.destroy()
    
    # Start application
    try:
        app = BackupPro()
        app.protocol("WM_DELETE_WINDOW", app.on_closing)
        app.mainloop()
    except Exception as e:
        logger.exception("Fatal error in main application")
        messagebox.showerror("Fatal Error", f"Application error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()