import json
import os
import sys

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QWidget, QFormLayout,
    QLineEdit, QPushButton, QHBoxLayout, QCheckBox,
    QDialogButtonBox, QFileDialog, QMessageBox, QLabel, QComboBox, QGroupBox, QGridLayout,
    QSizePolicy, QFrame, QScrollArea
)

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer

from constants import SETTINGS_FILE, DEFAULT_THEMES_PATH, DEFAULT_EXTRAS_PATH, CURRENT_VERSION, UPDATE_URL, ONLINE_FEATURES_AVAILABLE, crypted
from widgets import DragDropLineEdit


class _StorageThread(QThread):
    """Measures AI storage off the UI thread, so Settings opens at once."""
    result = pyqtSignal(object)

    def run(self):
        import icon_generator as ig
        try:
            self.result.emit(ig.storage_report())
        except Exception:
            self.result.emit(None)


class _CallThread(QThread):
    """Runs a function off the UI thread and emits its result (for the Vita
    connection test and the update check, which both hit the network)."""
    done = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as exc:
            self.done.emit(('error', str(exc)))


def _ftp_test(ip, port):
    """Try the Vita's FTP server; return (ok, message)."""
    import ftplib
    try:
        ftp = ftplib.FTP()
        ftp.connect(ip, port, timeout=8)
        ftp.login()
        ftp.quit()
        return (True, f"Connected to {ip}:{port}")
    except Exception as exc:
        return (False, f"Could not connect ({exc})")


def _latest_release():
    """The latest release from GitHub (dict with tag/body/assets), or None."""
    import app_updater
    return app_updater.fetch_latest()


class SettingsDialog(QDialog):
    def __init__(self, parent=None, ai_icons=False):
        super().__init__(parent)
        # The AI Icons tab only exists in builds with the icon generator.
        self.ai_icons = ai_icons
        self.setWindowTitle("⚙️ Settings")
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # --- General tab ---
        self.general_tab = QWidget()
        self.connection_tab = QWidget()
        self.tabs.addTab(self.general_tab, "General")
        self.tabs.addTab(self.connection_tab, "Connection")

        general_layout = QFormLayout()

        self.default_author_input = QLineEdit()
        general_layout.addRow("Default Author Name:", self.default_author_input)

        self.theme_path_input = QLineEdit()
        self.theme_path_input.setReadOnly(True)
        btn_browse_theme_path = QPushButton("Browse...")
        btn_browse_theme_path.clicked.connect(lambda: self.browse_path(self.theme_path_input, "Select Theme Library Folder"))
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.theme_path_input)
        path_layout.addWidget(btn_browse_theme_path)
        general_layout.addRow("Theme Library Path:", path_layout)
        theme_path_hint = QLabel("Tip: the default is a hidden app-data folder. Pick your own folder (e.g. in Documents) so themes are easy to find and back up. Existing themes are not moved automatically.")
        theme_path_hint.setWordWrap(True)
        theme_path_hint.setStyleSheet("color: grey; font-size: 10px; padding-bottom: 16px;")
        theme_path_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        general_layout.addRow(theme_path_hint)

        self.extras_path_input = QLineEdit()
        self.extras_path_input.setReadOnly(True)
        btn_browse_extras_path = QPushButton("Browse...")
        btn_browse_extras_path.clicked.connect(lambda: self.browse_path(self.extras_path_input, "Select Extras Library Folder"))
        extras_path_layout = QHBoxLayout()
        extras_path_layout.addWidget(self.extras_path_input)
        extras_path_layout.addWidget(btn_browse_extras_path)
        general_layout.addRow("Extras Library Path:", extras_path_layout)

        # at9tool.exe path — drag-and-drop or Browse
        self.at9tool_path_input = DragDropLineEdit(file_filter=['.exe'])
        self.at9tool_path_input.setPlaceholderText("Drag at9tool.exe here, or click Browse…")
        btn_browse_at9tool = QPushButton("Browse...")
        btn_browse_at9tool.clicked.connect(self._browse_at9tool)
        btn_clear_at9tool = QPushButton("Clear")
        btn_clear_at9tool.clicked.connect(lambda: self.at9tool_path_input.setText(''))
        at9tool_layout = QHBoxLayout()
        at9tool_layout.addWidget(self.at9tool_path_input)
        at9tool_layout.addWidget(btn_browse_at9tool)
        at9tool_layout.addWidget(btn_clear_at9tool)
        general_layout.addRow("at9tool.exe Path:", at9tool_layout)
        at9tool_hint = QLabel("Required for encoding BGM (WAV, MP3, FLAC, OGG, etc.) to AT9 (made by Sony, not included). Works on every OS. Drag the .exe onto the field above.")
        at9tool_hint.setWordWrap(True)
        at9tool_hint.setStyleSheet("color: grey; font-size: 10px; padding-bottom: 16px;")
        at9tool_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        general_layout.addRow(at9tool_hint)

        # Update settings live together in a footer pinned to the bottom of the
        # tab (above a thin separator): the auto-check option, then version +
        # project link on the left with the manual check on the right.
        self.auto_update_checkbox = QCheckBox("Check for updates automatically on startup")
        self.check_update_btn = QPushButton("Check for updates now")
        self.check_update_btn.clicked.connect(self._check_updates_now)
        self.update_status = QLabel("")
        repo_url = UPDATE_URL.split('/releases')[0]
        repo_link = QLabel(f'<a href="{repo_url}">{repo_url.replace("https://", "")}</a>')
        repo_link.setOpenExternalLinks(True)
        repo_link.setStyleSheet("color: grey;")
        # Quiet build-status note next to the version (grey, low-key — an official
        # build has the secure modules; anything else is unofficial).
        import app_updater
        _official = app_updater.running_frozen() and (crypted is not None)
        version_label = QLabel(f"Version {CURRENT_VERSION}  ·  "
                               + ("Official build" if _official else "Unofficial build"))
        version_label.setStyleSheet("color: grey;")
        # The in-app updater only runs in official builds (an unofficial/source
        # build can have a hand-edited version, so auto-updating it is unsafe).
        _official_tip = ("This is an official release build (it ships the signed "
                         "secure components), so in-app updates are available.")
        _unofficial_tip = ("This is an unofficial or source build (it doesn't ship "
                           "the secure components). In-app updates are turned off "
                           "because such a build can have a hand-edited version "
                           "number, so updating could replace it with a mismatched "
                           "release or run the wrong data migrations. Get official "
                           "builds from the releases page.")
        version_label.setToolTip(_official_tip if _official else _unofficial_tip)
        if not _official:
            self.auto_update_checkbox.setEnabled(False)
            self.check_update_btn.setEnabled(False)
            self.update_status.setText("Updates available in official builds only")
            self.update_status.setStyleSheet("color: grey;")
            self.update_status.setToolTip(_unofficial_tip)
            self.auto_update_checkbox.setToolTip(_unofficial_tip)
            self.check_update_btn.setToolTip(_unofficial_tip)
        separator = QFrame(); separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("background-color: #2e3a5c; max-height: 1px; border: none;")
        footer_row = QHBoxLayout()
        footer_row.addWidget(version_label); footer_row.addSpacing(14); footer_row.addWidget(repo_link)
        footer_row.addStretch()
        footer_row.addWidget(self.update_status); footer_row.addSpacing(10)
        footer_row.addWidget(self.auto_update_checkbox); footer_row.addSpacing(12)
        footer_row.addWidget(self.check_update_btn)
        general_outer = QVBoxLayout(self.general_tab)
        general_outer.addLayout(general_layout)

        general_outer.addStretch()          # push the footer to the bottom
        general_outer.addWidget(separator)
        general_outer.addLayout(footer_row)

        # --- Connection tab ---
        connection_layout = QFormLayout()
        self.vita_ip_input = QLineEdit()
        self.vita_ip_input.setPlaceholderText("e.g., 192.168.1.50")
        connection_layout.addRow("PS Vita IP Address:", self.vita_ip_input)
        self.vita_port_input = QLineEdit("1337")
        connection_layout.addRow("PS Vita Port:", self.vita_port_input)
        test_row = QHBoxLayout()
        self.test_conn_btn = QPushButton("Test Connection")
        self.test_conn_btn.setToolTip("Try connecting to the Vita's FTP server with the address and port above.")
        self.test_conn_btn.clicked.connect(self._test_connection)
        self.conn_status = QLabel("")
        test_row.addWidget(self.test_conn_btn); test_row.addWidget(self.conn_status); test_row.addStretch()

        # Step-by-step VitaShell FTP guide — fills the tab and tells the user
        # exactly what to do to get the IP and port above.
        guide_group = QGroupBox("How to connect to your PS Vita")
        # Lift the title clear of the box border and give it breathing room above
        # the first step.
        guide_group.setStyleSheet(
            "QGroupBox { margin-top: 18px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px 6px 4px; }")
        guide_layout = QVBoxLayout(guide_group)
        guide_layout.setSpacing(10)
        guide_layout.setContentsMargins(12, 18, 12, 12)   # extra top margin spaces the steps below the group title
        steps = [
            ("1.", "Connect the PS Vita and this PC to the <b>same Wi-Fi network</b>."),
            ("2.", "On the Vita, open <b>VitaShell</b> (install it with HENkaku/h-encore if you haven't)."),
            ("3.", "Press <b>Select</b> in VitaShell to start the FTP server."),
            ("4.", "VitaShell shows an address such as <b>ftp://192.168.1.50:1337</b>. Type the "
                   "IP (192.168.1.50) and port (1337) into the boxes above."),
            ("5.", "Click <b>Test Connection</b> to confirm it works, then use Send to Vita / Import "
                   "from Vita to transfer themes."),
        ]
        for num, text in steps:
            row = QHBoxLayout()
            num_label = QLabel(num)
            num_label.setStyleSheet("color: #4a90d9; font-weight: bold;")
            num_label.setFixedWidth(20)
            num_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            step_label = QLabel(text)
            step_label.setWordWrap(True)
            step_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            row.addWidget(num_label)
            row.addWidget(step_label, 1)
            guide_layout.addLayout(row)
        tip_label = QLabel("Tip: if the connection fails, make sure the FTP server is still running in "
                           "VitaShell (it stops when the app closes or the Vita sleeps) and that no "
                           "firewall is blocking this PC.")
        tip_label.setWordWrap(True)
        tip_label.setStyleSheet("color: grey; font-size: 10px;")
        tip_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        guide_layout.addWidget(tip_label)

        connection_outer = QVBoxLayout(self.connection_tab)
        connection_outer.addLayout(connection_layout)
        connection_outer.addSpacing(8)
        connection_outer.addLayout(test_row)
        connection_outer.addSpacing(8)
        connection_outer.addWidget(guide_group)
        connection_outer.addStretch()

        # --- AI Icons tab (builds with the icon generator only) ---
        if self.ai_icons:
            from icon_generator import GEMINI_MODELS, DEFAULT_GEMINI_MODEL
            self.ai_tab = QWidget()
            self.tabs.addTab(self.ai_tab, "AI Icons")
            ai_layout = QFormLayout()
            self.google_key_input = QLineEdit()
            self.google_key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.google_key_input.setPlaceholderText("Paste your Gemini API key")
            show_key = QCheckBox("Show")
            show_key.toggled.connect(lambda on: self.google_key_input.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
            self.google_key_input.setToolTip("Your Google Gemini API key, used only when the icon generator is set to Google Gemini.")
            show_key.setToolTip("Show the key while you check or edit it.")
            key_layout = QHBoxLayout()
            key_layout.addWidget(self.google_key_input)
            key_layout.addWidget(show_key)
            ai_layout.addRow("Google API Key:", key_layout)
            self.google_model_combo = QComboBox()
            for model, label, price in GEMINI_MODELS:
                self.google_model_combo.addItem(f"{label}, ~${price:.3f}/icon", model)
            self.default_google_model = DEFAULT_GEMINI_MODEL
            self.google_model_combo.setToolTip("Which Gemini image model makes the icons. Prices are per icon, billed to your key.")
            ai_layout.addRow("Gemini Image Model:", self.google_model_combo)
            ai_hint = QLabel("Used when the icon generator is set to Google Gemini. Get a key at "
                             "<a href='https://aistudio.google.com/apikey'>aistudio.google.com/apikey</a>. "
                             "Gemini image models are paid (no free tier): each icon is billed to this key. "
                             "The key is stored in this app's settings file on this PC.")
            ai_hint.setWordWrap(True)
            ai_hint.setOpenExternalLinks(True)
            ai_hint.setStyleSheet("color: grey; font-size: 10px; padding-bottom: 16px;")
            ai_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            ai_layout.addRow(ai_hint)   # sits right under the Gemini rows it describes

            # --- Local AI model (the on-this-PC generator) ---
            ai_layout.addRow(QLabel("<b>Local AI model (This PC)</b>"))
            self.custom_model_input = QLineEdit(); self.custom_model_input.setReadOnly(True)
            self.custom_model_input.setPlaceholderText("Default model (downloaded automatically)")
            self.custom_model_input.setToolTip("Use your own Stable Diffusion checkpoint (SD 1.5 or SDXL) instead of the "
                                               "default. A .safetensors file from another app works and is used in place (no copy).")
            btn_model_file = QPushButton("File…"); btn_model_file.clicked.connect(self._browse_custom_model_file)
            btn_model_folder = QPushButton("Folder…"); btn_model_folder.clicked.connect(self._browse_custom_model_folder)
            btn_model_clear = QPushButton("Clear"); btn_model_clear.clicked.connect(self.custom_model_input.clear)
            model_row = QHBoxLayout(); model_row.addWidget(self.custom_model_input)
            for b in (btn_model_file, btn_model_folder, btn_model_clear):
                model_row.addWidget(b)
            ai_layout.addRow("Custom model:", model_row)
            self.custom_arch_combo = QComboBox()
            for arch_label, arch_data in (("Auto-detect", "auto"), ("SD 1.5", "sd15"), ("SDXL", "sdxl")):
                self.custom_arch_combo.addItem(arch_label, arch_data)
            self.custom_arch_combo.setToolTip("Which architecture your custom model is. Auto-detect reads it from the "
                                              "file; set it manually if detection is wrong. Must match the quality tier you generate with.")
            ai_layout.addRow("Model type:", self.custom_arch_combo)
            self.shared_cache_input = QLineEdit(); self.shared_cache_input.setReadOnly(True)
            self.shared_cache_input.setPlaceholderText("Default (inside this app's data folder)")
            self.shared_cache_input.setToolTip("Reuse a Hugging Face model cache from another app (or another drive) to "
                                               "save disk space. Models already there are not downloaded again.")
            btn_cache_browse = QPushButton("Browse…"); btn_cache_browse.clicked.connect(self._browse_shared_cache)
            btn_cache_clear = QPushButton("Clear"); btn_cache_clear.clicked.connect(self.shared_cache_input.clear)
            cache_row = QHBoxLayout(); cache_row.addWidget(self.shared_cache_input)
            cache_row.addWidget(btn_cache_browse); cache_row.addWidget(btn_cache_clear)
            ai_layout.addRow("Shared model cache:", cache_row)
            self.hf_token_input = QLineEdit()
            self.hf_token_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.hf_token_input.setPlaceholderText("Only needed for the full FLUX.2 (Ultra+) model")
            self.hf_token_input.setToolTip("A Hugging Face access token, used only to download the gated full FLUX.2 "
                                           "(Ultra+) model. The 4-bit FLUX.2 and the other models need no token.")
            show_token = QCheckBox("Show")
            show_token.toggled.connect(lambda on: self.hf_token_input.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
            token_row = QHBoxLayout(); token_row.addWidget(self.hf_token_input); token_row.addWidget(show_token)
            ai_layout.addRow("Hugging Face token:", token_row)
            # Memory saving is automatic per GPU; this forces the least-memory path
            # for every local model, for when another app needs the GPU/RAM.
            self.min_memory_checkbox = QCheckBox("Minimal VRAM mode (offloads to system RAM to lower GPU memory; slower)")
            self.min_memory_checkbox.setToolTip("Keeps every local model (SD 1.5, SDXL and FLUX.2) using as little GPU "
                                                "memory (VRAM) as possible by offloading its layers to system RAM and "
                                                "streaming them to the GPU, and decoding the image in tiles. Uses more "
                                                "system RAM and is slower per icon, but frees the GPU for other apps.")
            ai_layout.addRow(self.min_memory_checkbox)
            # How much the AI parts take up, and removing one model setup while
            # the other keeps working (shared helper models stay).
            self.storage_group = QGroupBox("AI storage")
            # Reserve the table's height up front (it's measured in the background),
            # so the dialog opens at its final size — it centres correctly and
            # doesn't visibly resize when the measurement arrives.
            self.storage_group.setMinimumHeight(390)
            self.storage_layout = QVBoxLayout(self.storage_group)
            self.storage_content = None
            self.storage_thread = None
            self.storage_total = None
            self.refresh_storage()
            # Storage box sits below the form (not as a stretched form row); the
            # trailing stretch takes any spare height, keeping the rows tight.
            local_hint = QLabel("The local generator downloads a default model on first use. To use your own instead, "
                                "pick a .safetensors checkpoint (SD 1.5 or SDXL) — including one another app already "
                                "downloaded — and it's used in place, no copy. A shared cache lets this app reuse models "
                                "other apps downloaded. It applies when its type matches the quality tier you generate with.")
            local_hint.setWordWrap(True)
            local_hint.setStyleSheet("color: grey; font-size: 10px; padding-bottom: 16px;")
            local_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

            # The AI tab has many rows plus the storage table; put it in a scroll
            # area so it doesn't force the whole Settings window to be very tall.
            ai_content = QWidget()
            ai_outer = QVBoxLayout(ai_content)
            ai_outer.addLayout(ai_layout)
            ai_outer.addWidget(local_hint)          # right under the local-model rows
            ai_outer.addWidget(self.storage_group)
            ai_outer.addStretch()
            ai_scroll = QScrollArea()
            ai_scroll.setWidgetResizable(True)
            ai_scroll.setFrameShape(QFrame.Shape.NoFrame)
            ai_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ai_scroll.setWidget(ai_content)
            ai_tab_layout = QVBoxLayout(self.ai_tab)
            ai_tab_layout.setContentsMargins(0, 0, 0, 0)
            ai_tab_layout.addWidget(ai_scroll)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.load_settings()
        self._lock_tab_size()

    def _test_connection(self):
        ip = self.vita_ip_input.text().strip()
        port = self.vita_port_input.text().strip() or "1337"
        if not ip:
            self.conn_status.setText("Enter the Vita's IP address first.")
            self.conn_status.setStyleSheet("color: #e5645f;")
            return
        try:
            port_num = int(port)
        except ValueError:
            self.conn_status.setText("Port must be a number.")
            self.conn_status.setStyleSheet("color: #e5645f;")
            return
        self.test_conn_btn.setEnabled(False)
        self.conn_status.setText("Testing…"); self.conn_status.setStyleSheet("color: grey;")
        self._conn_thread = _CallThread(lambda: _ftp_test(ip, port_num), self)
        self._conn_thread.done.connect(self._connection_tested)
        self._conn_thread.start()

    def _connection_tested(self, result):
        self.test_conn_btn.setEnabled(True)
        ok, message = result if isinstance(result, tuple) and len(result) == 2 else (False, "Test failed")
        self.conn_status.setText(("✓ " if ok else "✗ ") + message)
        self.conn_status.setStyleSheet("color: #4cb86a;" if ok else "color: #e5645f;")

    def _check_updates_now(self):
        self.check_update_btn.setEnabled(False)
        self.update_status.setText("Checking…"); self.update_status.setStyleSheet("color: grey;")
        self._update_thread = _CallThread(_latest_release, self)
        self._update_thread.done.connect(self._update_checked)
        self._update_thread.start()

    def _update_checked(self, result):
        import app_updater
        self.check_update_btn.setEnabled(True)
        if not isinstance(result, dict) or (isinstance(result, tuple) and result and result[0] == 'error'):
            self.update_status.setText("Couldn't check for updates.")
            self.update_status.setStyleSheet("color: #e5645f;")
            return
        rel = result
        tag = rel.get('tag') or ''
        if not tag:
            self.update_status.setText("Couldn't check for updates.")
            self.update_status.setStyleSheet("color: #e5645f;")
            return
        if app_updater.is_newer(tag, CURRENT_VERSION):
            self.update_status.setText(f"Update available: {tag}")
            self.update_status.setStyleSheet("color: #4cb86a;")
            parent = self.parent()
            if parent is not None and hasattr(parent, 'offer_update'):
                self.accept()  # close Settings so the update dialog is in front
                parent.offer_update(rel)
        else:
            self.update_status.setText("You're up to date.")
            self.update_status.setStyleSheet("color: grey;")

    def _lock_tab_size(self):
        """Pin the tab area to the tallest tab's size hint, so switching tabs
        doesn't resize the window (only ever grows the floor, never shrinks)."""
        try:
            for i in range(self.tabs.count()):
                page_layout = self.tabs.widget(i).layout()
                if page_layout:
                    page_layout.invalidate(); page_layout.activate()   # so the size hint is current
            width = max(self.tabs.widget(i).sizeHint().width() for i in range(self.tabs.count()))
            height = max(self.tabs.widget(i).sizeHint().height() for i in range(self.tabs.count()))
            self.tabs.setMinimumSize(max(width, self.tabs.minimumWidth()),
                                     max(height, self.tabs.minimumHeight()))
        except Exception:
            pass

    def refresh_storage(self):
        """Measure AI storage in the background; the table fills in when done."""
        self._set_storage_content().addWidget(QLabel("Measuring the AI parts on disk..."))
        thread = _StorageThread(self)
        thread.result.connect(lambda report: self._show_storage(report) if thread is self.storage_thread else None)
        if self.storage_thread:
            self.storage_thread.wait()   # an older measurement: its result is ignored
        self.storage_thread = thread
        thread.start()

    def _set_storage_content(self):
        """Replace the storage box's contents; returns the new layout."""
        if self.storage_content is not None:
            self.storage_content.deleteLater()
        self.storage_content = QWidget()
        self.storage_layout.addWidget(self.storage_content)
        box = QVBoxLayout(self.storage_content)
        box.setContentsMargins(0, 0, 0, 0)
        return box

    def _show_storage(self, report):
        """The AI storage table: part, size, and a Remove button for each model
        and add-on."""
        import icon_generator as ig
        box = self._set_storage_content()
        if report is None:
            box.addWidget(QLabel("The AI storage could not be measured."))
            return
        self.storage_total = report['total']
        if not report['total']:
            box.addWidget(QLabel("No AI components are installed."))
            return
        total = QLabel(f"Total: <b>{ig.format_size(report['total'])}</b> in {ig.ICON_AI_DIR}")
        total.setWordWrap(True)
        box.addWidget(total)
        shared = report.get('shared')
        if shared:
            note = QLabel(f"Models are in a shared cache: {report.get('models_dir')} — manage them there "
                          "(they may be used by other apps, so this app won't delete them).")
            note.setWordWrap(True)
            note.setStyleSheet("color: grey; font-size: 10px;")
            box.addWidget(note)
        table = QGridLayout()
        table.setHorizontalSpacing(18)
        table.setVerticalSpacing(6)
        table.setColumnStretch(0, 1)   # the name column takes the slack, pushing size + Remove to the right
        # Memory estimates shown per model reflect the current Minimal VRAM setting.
        min_mem = bool(ig._settings().get('ai_min_memory'))
        # (name, size, remove-callback, model-key-for-memory-estimate)
        rows = [("PyTorch and AI libraries (plus a small ~140 MB Python to run them)", report['runtime'], None, None)]
        for model, poses, label, size, installed in report['setups']:
            if poses:
                label += " (only for the experimental AI poses option)"
            rows.append((label, size if installed or size else 0,
                         lambda _c=False, m=model, p=poses, l=label: self.remove_setup(m, p, l),
                         None if poses else model))
        for variant, label, size, installed in report.get('flux2', []):
            rows.append((label, size if installed else 0,
                         (lambda _c=False, v=variant, l=label: self.remove_flux2_tier(v, l)) if installed else None,
                         variant))
        rows += [("Shared helper models (needed by both models)", report['helpers'], None, None),
                 ("Other (example icon sets, extra artwork, caches)", report['other'], None, None)]
        for r, (text, size, remove, model_key) in enumerate(rows):
            if model_key:
                text = (f"{text}<br><span style='color:grey; font-size:9px'>"
                        f"{ig.memory_note(model_key, min_mem)}</span>")
            name_label = QLabel(text)
            name_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            table.addWidget(name_label, r, 0)
            size_label = QLabel(ig.format_size(size) if size else "not installed")
            size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.addWidget(size_label, r, 1)
            if remove:
                button = QPushButton("Remove")
                # Don't offer to delete models that live in a shared cache: they
                # may belong to another app.
                button.setEnabled(bool(size) and not shared)
                if shared:
                    button.setToolTip("These models are in a shared cache; remove them from that folder.")
                button.setFixedHeight(30)   # keep button rows the same height as the others
                button.clicked.connect(remove)
                table.addWidget(button, r, 2)
            # Model rows carry a second (memory) line, so they're a little taller.
            table.setRowMinimumHeight(r, 44 if model_key else 34)
        box.addLayout(table)
        everything = QPushButton("Remove Everything")
        everything.setToolTip("Delete the AI runtime and all models. You can install them again from the icon generator.")
        everything.clicked.connect(self.remove_all_ai)
        line = QHBoxLayout(); line.addWidget(everything); line.addStretch()
        box.addLayout(line)
        # Calibration data: anonymous timing/memory measurements from each run,
        # used to sharpen the estimates (and, by comparing hardware, predict
        # other machines). Offer to export it so it can be uploaded to improve a
        # later update.
        count = ig.calibration_count()
        if count:
            cal_row = QHBoxLayout()
            cal_note = QLabel(f"Performance data: {count} de-duplicated record{'s' if count != 1 else ''} "
                              "(hardware, settings, timing and memory — no personal or network data).")
            cal_note.setWordWrap(True)
            cal_note.setStyleSheet("color: grey; font-size: 10px;")
            cal_row.addWidget(cal_note, 1)
            # Sharing is an online feature: only official builds (with the secure
            # online modules) offer it. User/source builds still record data to
            # improve their own estimates, but have no share button.
            if ig.ONLINE_FEATURES_AVAILABLE:
                cal_share = QPushButton("Share with creator…")
                cal_share.setToolTip("Save a validated, de-duplicated copy of your performance data so you can send it to "
                                     "the creator. It helps make the time and memory estimates more accurate for your "
                                     "hardware (and similar setups). Sending it is entirely up to you.")
                cal_share.clicked.connect(self._share_calibration)
                cal_row.addWidget(cal_share)
            box.addLayout(cal_row)
        # The AI tab is now at its full height; re-pin so it stays this size.
        self._lock_tab_size()

    def _share_calibration(self):
        import icon_generator as ig
        dest, _ = QFileDialog.getSaveFileName(self, "Share Performance Data With Creator", "vta_performance_data.jsonl",
                                              "Data files (*.jsonl)")
        if not dest:
            return
        if not dest.lower().endswith('.jsonl'):
            dest += '.jsonl'   # keep it to the one accepted type
        try:
            written = ig.export_calibration(dest)
        except OSError as e:
            QMessageBox.warning(self, "Could not save", f"The data could not be saved:\n{e}")
            return
        # No credentials can safely live in the app, so sending is manual: save
        # the file, reveal it, and tell the user how to pass it on.
        box = QMessageBox(self)
        box.setWindowTitle("Ready to share")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Saved {written} validated, de-duplicated record{'s' if written != 1 else ''} to:\n{dest}\n\n"
                    "Send this file to the creator (for example by email or by attaching it to a GitHub issue) to help "
                    "improve the estimates. It contains only hardware details, the settings used and the measured "
                    "time and memory — no personal or network data.")
        reveal = box.addButton("Show file", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is reveal:
            self._reveal_file(dest)

    def _reveal_file(self, path):
        import subprocess
        try:
            if sys.platform == 'win32':
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', path])
            else:
                subprocess.Popen(['xdg-open', os.path.dirname(path)])
        except OSError:
            pass

    def remove_setup(self, model, poses, label):
        import icon_generator as ig
        extra = "" if poses else " Its AI poses add-on is removed too."
        if QMessageBox.question(self, "Remove AI Model",
                                f"Delete the {label}?{extra}\n\nEverything the other model still needs is kept, "
                                "so it keeps working. You can download it again from the icon generator.") != QMessageBox.StandardButton.Yes:
            return
        ig.remove_model_setup(model, poses)
        self.refresh_storage()

    def remove_flux2_tier(self, variant, label):
        import icon_generator as ig
        if QMessageBox.question(self, "Remove AI Model",
                                f"Delete the {label}?\n\nYou can download it again from the icon generator.") \
                != QMessageBox.StandardButton.Yes:
            return
        ig.remove_flux2(variant)
        self.refresh_storage()

    def remove_all_ai(self):
        import icon_generator as ig
        if QMessageBox.question(self, "Remove All AI Components",
                                f"Delete the AI runtime and every model ({ig.format_size(self.storage_total if self.storage_total is not None else ig.runtime_disk_usage())})?\n\n{ig.ICON_AI_DIR}") \
                != QMessageBox.StandardButton.Yes:
            return
        ig.remove_runtime()
        self.refresh_storage()

    def _browse_at9tool(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select at9tool.exe", self.at9tool_path_input.text(),
            "Executable (*.exe);;All Files (*.*)"
        )
        if path:
            self.at9tool_path_input.setText(path)

    def browse_path(self, line_edit, title):
        path = QFileDialog.getExistingDirectory(self, title, line_edit.text())
        if path: line_edit.setText(path)

    def _browse_custom_model_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a model checkpoint", self.custom_model_input.text(),
            "Model checkpoints (*.safetensors *.ckpt);;All Files (*.*)")
        if path: self.custom_model_input.setText(path)

    def _browse_custom_model_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select a diffusers model folder", self.custom_model_input.text())
        if path: self.custom_model_input.setText(path)

    def _browse_shared_cache(self):
        path = QFileDialog.getExistingDirectory(self, "Select a Hugging Face cache folder", self.shared_cache_input.text())
        if path: self.shared_cache_input.setText(path)

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, 'r') as f: settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): settings = {}
        default_theme_path = DEFAULT_THEMES_PATH
        default_extras_path = DEFAULT_EXTRAS_PATH
        self.default_author_input.setText(settings.get('default_author', ''))
        self.theme_path_input.setText(settings.get('theme_library_path', default_theme_path))
        self.extras_path_input.setText(settings.get('extras_library_path', default_extras_path))
        self.at9tool_path_input.setText(settings.get('at9tool_path', ''))
        self.vita_ip_input.setText(settings.get('vita_ip', ''))
        self.vita_port_input.setText(settings.get('vita_port', '1337'))
        self.auto_update_checkbox.setChecked(settings.get('auto_update_check', True))
        if self.ai_icons:
            self.google_key_input.setText(settings.get('google_api_key', ''))
            model = settings.get('google_image_model') or self.default_google_model
            self.google_model_combo.setCurrentIndex(max(0, self.google_model_combo.findData(model)))
            self.custom_model_input.setText(settings.get('custom_base_model', ''))
            self.custom_arch_combo.setCurrentIndex(max(0, self.custom_arch_combo.findData(settings.get('custom_base_arch', 'auto'))))
            self.shared_cache_input.setText(settings.get('shared_model_cache', ''))
            self.hf_token_input.setText(settings.get('hf_token', ''))
            self.min_memory_checkbox.setChecked(settings.get('ai_min_memory', False))
        if 'theme_library_path' not in settings or 'extras_library_path' not in settings:
            self.save_settings()

    def save_settings(self):
        # Keep keys this dialog doesn't manage (e.g. library_location_prompted)
        try:
            with open(SETTINGS_FILE, 'r') as f: settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): settings = {}
        settings.update({
            'default_author': self.default_author_input.text(),
            'theme_library_path': self.theme_path_input.text(),
            'extras_library_path': self.extras_path_input.text(),
            'at9tool_path': self.at9tool_path_input.text(),
            'vita_ip': self.vita_ip_input.text(),
            'vita_port': self.vita_port_input.text(),
            'auto_update_check': self.auto_update_checkbox.isChecked(),
        })
        if self.ai_icons:
            settings['google_api_key'] = self.google_key_input.text().strip()
            settings['google_image_model'] = self.google_model_combo.currentData() or self.default_google_model
            settings['custom_base_model'] = self.custom_model_input.text().strip()
            settings['custom_base_arch'] = self.custom_arch_combo.currentData() or 'auto'
            settings['shared_model_cache'] = self.shared_cache_input.text().strip()
            settings['hf_token'] = self.hf_token_input.text().strip()
            settings['ai_min_memory'] = self.min_memory_checkbox.isChecked()
        try:
            with open(SETTINGS_FILE, 'w') as f: json.dump(settings, f, indent=4)
            return True
        except IOError as e: QMessageBox.warning(self, "Error", f"Could not save settings: {e}"); return False
    def showEvent(self, event):
        super().showEvent(event)
        # Centre once the final size is settled (deferring avoids centring on an
        # intermediate size, which left the window off-centre).
        QTimer.singleShot(0, self._centre_on_parent)

    def _centre_on_parent(self):
        from widgets import center_on_parent
        center_on_parent(self)
    def done(self, result):
        # A background task still running would crash when the window goes.
        for attr in ('storage_thread', '_conn_thread', '_update_thread'):
            thread = getattr(self, attr, None)
            if thread is not None:
                thread.wait()
        super().done(result)

    def accept(self):
        if self.save_settings(): os.makedirs(self.theme_path_input.text(), exist_ok=True); os.makedirs(self.extras_path_input.text(), exist_ok=True); QMessageBox.information(self, "Success", "Settings saved successfully!"); super().accept()
