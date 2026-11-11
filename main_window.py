import sys
import os
import json
import shutil
import zipfile
import xml.etree.ElementTree as ET
from xml.dom import minidom
from PIL import Image
import requests
import datetime
import re
import ftplib
import io
import tempfile
import time
import webview
import base64

from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QHBoxLayout, QFrame, QTabWidget,
    QListWidgetItem, QDialog, QDialogButtonBox,
    QFormLayout, QMessageBox, QFileDialog, QColorDialog, QScrollArea,
    QProgressDialog, QComboBox, QListWidget, QAbstractItemView,
    QSlider, QStyle, QCheckBox, QSizePolicy, QInputDialog, QMenu, QTextBrowser
)
from PySide6.QtGui import QFont, QIcon, QColor, QPixmap, QIntValidator, QAction, QKeySequence, QDesktopServices
from PySide6.QtCore import Qt, QSize, QUrl, QTimer, QDir, QPoint, QStandardPaths
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from constants import CURRENT_VERSION, UPDATE_URL, SETTINGS_FILE, DEFAULT_THEMES_PATH, DEFAULT_EXTRAS_PATH, crypted, ONLINE_FEATURES_AVAILABLE, ONLINE_FEATURES_UNAVAILABLE_MSG, theme_pages, store_path, load_path
import app_updater
from audio import (convert_at9_to_wav, convert_to_at9, removeTemp, AudioPlayerDialog, find_at9tool, AT9TOOL_REQUIRED_MSG, ENCODABLE_AUDIO_EXTS)
from wine_setup import shutdown_wine
from widgets import ColorPickerButton, play_done_sound
from settings_dialog import SettingsDialog
from about_dialog import AboutDialog
from theme_downloader import ThemeDownloaderDialog, ThemeExistsDialog
from vita_importer import VitaImporterDialog
# Optional build feature: only compiled in when setup_build.py enables icon AI.
try:
    from icon_generator import IconGeneratorDialog
    ICON_AI_AVAILABLE = True
except ImportError:
    ICON_AI_AVAILABLE = False


# =============================================================================
# == MAIN APPLICATION WINDOW CLASS
# =============================================================================
class VitaThemeArchitect(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("🎨 VitaTheme Architect"); self.setGeometry(100, 100, 1200, 700)
        current = self.geometry()
        width = current.width()
        height = current.height()
        #  self.setMinimumSize(width, height)
        self.theme_library_path = ""; self.extras_library_path = ""; self.current_theme_path = None; self.asset_widgets = {}
        self.manager_actions = []
        self.icon_gen_dirs = []  # temporary AI-generated icon folders the theme editor is using
        # Leftovers from a crash: unused generated icons and community theme
        # art older than a few hours (not another window's current work).
        tmp = tempfile.gettempdir()
        for name in os.listdir(tmp):
            folder = os.path.join(tmp, name)
            if name.startswith(('vta_icons_', 'vta_community_art_')) and os.path.isdir(folder):
                try:
                    if time.time() - os.path.getmtime(folder) > 6 * 3600:
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    pass
        self.temp_zip_for_upload = None # For repository submission
        self.central_widget = QWidget(); self.setCentralWidget(self.central_widget); self.main_layout = QHBoxLayout(self.central_widget)
        # Migrate local data formats if this build is newer than the one that
        # last wrote it (e.g. after an update). Never deletes user data.
        try:
            import data_migrations
            applied = data_migrations.run_migrations()
            if applied:
                print(f"Applied data migrations: {', '.join(applied)}")
        except Exception as e:
            print(f"Data migration step skipped: {e}")
        self.load_paths_from_settings()
        self.show_manager_ui()
        self.showMaximized()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self.recommend_library_location)
        # Run the startup update check only after the main window is shown, so
        # the dialog has a visible parent (and a taskbar entry) behind it.
        QTimer.singleShot(0, self.check_for_updates)

    def save_settings_file(self):
        try:
            with open(SETTINGS_FILE, 'w') as f: json.dump(self.settings, f, indent=4)
        except OSError as e: print(f"Could not save settings: {e}")

    def recommend_library_location(self):
        """On first run, recommends moving the theme library out of the hidden app-data folder."""
        if self.settings.get('library_location_prompted'): return
        if os.path.normcase(os.path.abspath(self.theme_library_path)) != os.path.normcase(os.path.abspath(DEFAULT_THEMES_PATH)):
            self.settings['library_location_prompted'] = True; self.save_settings_file(); return
        msg = QMessageBox(self); msg.setIcon(QMessageBox.Icon.Information); msg.setWindowTitle("Choose a Theme Library Folder")
        msg.setText("Your themes are currently stored in the app's hidden data folder:\n\n"
                    f"{self.theme_library_path}\n\n"
                    "It is recommended to pick your own folder (for example a \"Vita Themes\" folder in Documents) "
                    "so your themes are easy to find, back up and keep if the app is removed.\n\n"
                    "You can change this later in Settings.")
        btn_choose = msg.addButton("Choose Folder...", QMessageBox.ButtonRole.AcceptRole)
        btn_keep = msg.addButton("Keep Default", QMessageBox.ButtonRole.RejectRole)
        msg.addButton("Ask Me Later", QMessageBox.ButtonRole.DestructiveRole)
        msg.setDefaultButton(btn_choose); msg.exec()
        clicked = msg.clickedButton()
        if clicked == btn_keep:
            self.settings['library_location_prompted'] = True; self.save_settings_file()
        elif clicked == btn_choose:
            if self.change_theme_library_location():
                self.settings['library_location_prompted'] = True; self.save_settings_file()

    def change_theme_library_location(self):
        """Lets the user pick a new theme library folder, optionally moving existing themes. Returns True if changed."""
        start_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or os.path.expanduser('~')
        new_path = QFileDialog.getExistingDirectory(self, "Select Theme Library Folder", start_dir)
        if not new_path: return False
        new_path = os.path.abspath(new_path); old_path = os.path.abspath(self.theme_library_path)
        if os.path.normcase(new_path) == os.path.normcase(old_path): return True
        try:
            os.makedirs(new_path, exist_ok=True)
            if not os.access(new_path, os.W_OK): raise PermissionError("folder is not writable")
        except OSError as e:
            QMessageBox.critical(self, "Error", f"Cannot use this folder: {e}"); return False
        existing = [d for d in os.listdir(old_path) if os.path.isdir(os.path.join(old_path, d))] if os.path.isdir(old_path) else []
        if existing:
            reply = QMessageBox.question(self, "Move Existing Themes", f"Move your {len(existing)} existing theme folder(s) to the new location?\n\n{new_path}",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                failed = []
                for name in existing:
                    target = os.path.join(new_path, name); counter = 1
                    while os.path.exists(target): target = os.path.join(new_path, f"{name}{counter}"); counter += 1
                    try: shutil.move(os.path.join(old_path, name), target)
                    except Exception as e: failed.append(f"{name}: {e}")
                if failed: QMessageBox.warning(self, "Move Themes", "Some themes could not be moved:\n " + '\n '.join(failed))
        self.settings['theme_library_path'] = store_path(new_path); self.save_settings_file()
        self.show_manager_ui()
        return True

    def load_paths_from_settings(self):
        # Loads configuration from settings.json
        try:
            with open(SETTINGS_FILE, 'r') as f: self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): self.settings = {}
        default_theme_path = DEFAULT_THEMES_PATH; default_extras_path = DEFAULT_EXTRAS_PATH
        # load_path resolves a relative (portable) value against the current data
        # base, so it keeps working after a USB drive letter / volume change.
        self.theme_library_path = load_path(self.settings.get('theme_library_path', ''), default_theme_path)
        self.extras_library_path = load_path(self.settings.get('extras_library_path', ''), default_extras_path)
        if 'theme_library_path' not in self.settings or 'extras_library_path' not in self.settings:
            self.settings['theme_library_path'] = store_path(self.theme_library_path); self.settings['extras_library_path'] = store_path(self.extras_library_path)
            with open(SETTINGS_FILE, 'w') as f: json.dump(self.settings, f, indent=4)
        os.makedirs(self.theme_library_path, exist_ok=True); os.makedirs(self.extras_library_path, exist_ok=True)

    # -------------------------------------------------------------------------
    # UI Initialization
    # -------------------------------------------------------------------------
    def show_manager_ui(self):
        """Builds and displays the primary Theme Manager interface."""
        # Leaving the theme editor (saved or not): its generated icons go.
        self.clear_generated_icons()
        # Clear existing manager actions
        for action in self.manager_actions:
            self.removeAction(action)
            action.deleteLater()
        self.manager_actions = []

        while self.main_layout.count():
            widget = self.main_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            del widget
        self.load_paths_from_settings()
        library_panel = QFrame(); library_layout = QVBoxLayout(); library_layout.addWidget(QLabel(f"<b>My Themes Library (Location: {self.theme_library_path})</b>"))
        search_layout = QHBoxLayout(); self.local_search_bar = QLineEdit(); self.local_search_bar.setPlaceholderText("Search..."); self.local_search_bar.textChanged.connect(self.filter_local_themes)
        self.local_search_combo = QComboBox(); self.local_search_combo.addItems(["Search by Title", "Search by Author"]); self.local_search_combo.currentIndexChanged.connect(self.filter_local_themes)
        search_layout.addWidget(self.local_search_bar); search_layout.addWidget(self.local_search_combo); library_layout.addLayout(search_layout)
        sort_layout = QHBoxLayout(); sort_layout.addWidget(QLabel("Sort by:"))
        self.local_sort_combo = QComboBox(); self.local_sort_combo.addItems(["Alphabetical (A-Z)", "Alphabetical (Z-A)", "Author (A-Z)", "Author (Z-A)", "Version (High-Low)", "Version (Low-High)"]); self.local_sort_combo.currentIndexChanged.connect(self.sort_local_themes)
        sort_layout.addWidget(self.local_sort_combo); library_layout.addLayout(sort_layout)
        self.theme_list = QListWidget(); self.theme_list.setIconSize(QSize(128, 72))
        self.theme_list.itemSelectionChanged.connect(self.on_selection_changed); library_layout.addWidget(self.theme_list); library_panel.setLayout(library_layout)
        actions_panel = QFrame(); actions_panel.setFrameShape(QFrame.Shape.StyledPanel); actions_panel.setProperty("role", "panel"); actions_layout = QVBoxLayout()

        # --- Library Actions ---
        actions_layout.addWidget(QLabel("<b>Library Actions</b>"))
        self.btn_new_theme = QPushButton("✨ New Theme (Ctrl+N)")
        self.btn_new_theme.setProperty("role", "primary"); self.btn_new_theme.clicked.connect(lambda: self.show_creator_ui()); actions_layout.addWidget(self.btn_new_theme)
        new_theme_action = QAction("New Theme", self); new_theme_action.setShortcut(QKeySequence("Ctrl+N"))
        new_theme_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        new_theme_action.triggered.connect(lambda: self.show_creator_ui()); self.addAction(new_theme_action); self.manager_actions.append(new_theme_action)

        self.btn_import_zip = QPushButton("📥 Import Theme (.zip) (Ctrl+I)")
        self.btn_import_zip.clicked.connect(self.import_zip_dialog); actions_layout.addWidget(self.btn_import_zip)
        import_zip_action = QAction("Import Zip", self); import_zip_action.setShortcut(QKeySequence("Ctrl+I"))
        import_zip_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        import_zip_action.triggered.connect(self.import_zip_dialog); self.addAction(import_zip_action); self.manager_actions.append(import_zip_action)

        self.btn_import_folder = QPushButton("📂 Import Theme (Folder) (Ctrl+O)")
        self.btn_import_folder.clicked.connect(self.import_folder); actions_layout.addWidget(self.btn_import_folder)
        import_folder_action = QAction("Import Folder", self); import_folder_action.setShortcut(QKeySequence("Ctrl+O"))
        import_folder_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        import_folder_action.triggered.connect(self.import_folder); self.addAction(import_folder_action); self.manager_actions.append(import_folder_action)

        self.btn_bulk_import = QPushButton("📚 Import Multiple Themes (Ctrl+Shift+I)")
        bulk_menu = QMenu(self.btn_bulk_import)
        bulk_menu.addAction("From a folder of themes / zips...", self.bulk_import_folder_dialog)
        bulk_menu.addAction("From a zip of themes...", self.bulk_import_zip_dialog)
        self.btn_bulk_import.setMenu(bulk_menu); actions_layout.addWidget(self.btn_bulk_import)
        bulk_import_action = QAction("Import Multiple Themes", self); bulk_import_action.setShortcut(QKeySequence("Ctrl+Shift+I"))
        bulk_import_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        bulk_import_action.triggered.connect(self.btn_bulk_import.showMenu); self.addAction(bulk_import_action); self.manager_actions.append(bulk_import_action)

        self.btn_import_vita = QPushButton("📡 Import from PS Vita (Ctrl+V)")
        self.btn_import_vita.clicked.connect(self.import_from_vita); actions_layout.addWidget(self.btn_import_vita)
        import_vita_action = QAction("Import from Vita", self); import_vita_action.setShortcut(QKeySequence("Ctrl+V"))
        import_vita_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        import_vita_action.triggered.connect(self.import_from_vita); self.addAction(import_vita_action); self.manager_actions.append(import_vita_action)

        self.btn_download_themes = QPushButton("🌐 Download Themes (Ctrl+D)")
        self.btn_download_themes.clicked.connect(self.open_downloader); actions_layout.addWidget(self.btn_download_themes)
        download_themes_action = QAction("Download Themes", self); download_themes_action.setShortcut(QKeySequence("Ctrl+D"))
        download_themes_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        download_themes_action.triggered.connect(self.open_downloader); self.addAction(download_themes_action); self.manager_actions.append(download_themes_action)
        if not ONLINE_FEATURES_AVAILABLE:
            self.btn_download_themes.setEnabled(False); download_themes_action.setEnabled(False)
            self.btn_download_themes.setToolTip(ONLINE_FEATURES_UNAVAILABLE_MSG)

        # --- Selected Theme Actions ---
        actions_layout.addSpacing(40); actions_layout.addWidget(QLabel("<b>Selected Theme Actions</b>"))

        self.btn_edit_theme = QPushButton("✏️ Edit Selected Theme (Ctrl+E)")
        self.btn_edit_theme.clicked.connect(self.edit_theme); actions_layout.addWidget(self.btn_edit_theme)
        self.edit_theme_action = QAction("Edit Theme", self); self.edit_theme_action.setShortcut(QKeySequence("Ctrl+E"))
        self.edit_theme_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        self.edit_theme_action.triggered.connect(self.edit_theme); self.addAction(self.edit_theme_action); self.manager_actions.append(self.edit_theme_action)

        self.btn_play_bgm = QPushButton("🎵 Play BGM (Ctrl+P)")
        self.btn_play_bgm.clicked.connect(self.play_bgm); actions_layout.addWidget(self.btn_play_bgm)
        self.play_bgm_action = QAction("Play BGM", self); self.play_bgm_action.setShortcut(QKeySequence("Ctrl+P"))
        self.play_bgm_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        self.play_bgm_action.triggered.connect(self.play_bgm); self.addAction(self.play_bgm_action); self.manager_actions.append(self.play_bgm_action)

        self.btn_extract_bgm = QPushButton("💾 Extract BGM (Ctrl+X)")
        self.btn_extract_bgm.clicked.connect(self.extract_bgm); actions_layout.addWidget(self.btn_extract_bgm)
        self.extract_bgm_action = QAction("Extract BGM", self); self.extract_bgm_action.setShortcut(QKeySequence("Ctrl+X"))
        self.extract_bgm_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        self.extract_bgm_action.triggered.connect(self.extract_bgm); self.addAction(self.extract_bgm_action); self.manager_actions.append(self.extract_bgm_action)

        self.btn_send_to_vita = QPushButton("➡️ Send to Vita (Ctrl+S)")
        self.btn_send_to_vita.clicked.connect(self.send_to_vita); actions_layout.addWidget(self.btn_send_to_vita)
        self.send_to_vita_action = QAction("Send to Vita", self); self.send_to_vita_action.setShortcut(QKeySequence("Ctrl+S"))
        self.send_to_vita_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        self.send_to_vita_action.triggered.connect(self.send_to_vita); self.addAction(self.send_to_vita_action); self.manager_actions.append(self.send_to_vita_action)

        # --- NEW SUBMIT BUTTON ---
        self.btn_submit_theme = QPushButton("🚀 Submit Theme to Repository")
        self.btn_submit_theme.clicked.connect(self.submit_theme_to_repo); actions_layout.addWidget(self.btn_submit_theme)
        self.submit_theme_action = QAction("Submit Theme", self)
        self.submit_theme_action.triggered.connect(self.submit_theme_to_repo); self.addAction(self.submit_theme_action); self.manager_actions.append(self.submit_theme_action)
        # --- END NEW BUTTON ---

        self.btn_delete_theme = QPushButton("🗑️ Delete Selected Theme (Del)")
        self.btn_delete_theme.setProperty("role", "danger"); self.btn_delete_theme.clicked.connect(self.delete_theme); actions_layout.addWidget(self.btn_delete_theme)
        self.delete_theme_action = QAction("Delete Theme", self); self.delete_theme_action.setShortcut(QKeySequence("Delete"))
        self.delete_theme_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut) # <-- ADDED
        self.delete_theme_action.triggered.connect(self.delete_theme); self.addAction(self.delete_theme_action); self.manager_actions.append(self.delete_theme_action)

        # --- Disable actions by default ---
        self.btn_edit_theme.setEnabled(False); self.edit_theme_action.setEnabled(False)
        self.btn_play_bgm.setEnabled(False); self.play_bgm_action.setEnabled(False)
        self.btn_extract_bgm.setEnabled(False); self.extract_bgm_action.setEnabled(False)
        self.btn_send_to_vita.setEnabled(False); self.send_to_vita_action.setEnabled(False)
        self.btn_submit_theme.setEnabled(False); self.submit_theme_action.setEnabled(False) # <-- Disable new button
        if not ONLINE_FEATURES_AVAILABLE: self.btn_submit_theme.setToolTip(ONLINE_FEATURES_UNAVAILABLE_MSG)
        self.btn_delete_theme.setEnabled(False); self.delete_theme_action.setEnabled(False)

        actions_layout.addStretch()
        bottom_buttons_layout = QHBoxLayout(); self.btn_about = QPushButton("ℹ️"); self.btn_about.setFixedSize(QSize(30, 30)); self.btn_about.setToolTip("About VitaTheme Architect"); self.btn_about.clicked.connect(self.open_about); bottom_buttons_layout.addWidget(self.btn_about); bottom_buttons_layout.addStretch(); self.btn_settings = QPushButton("⚙️ Settings"); self.btn_settings.clicked.connect(self.open_settings); bottom_buttons_layout.addWidget(self.btn_settings)
        actions_layout.addLayout(bottom_buttons_layout); actions_panel.setLayout(actions_layout)
        self.main_layout.addWidget(library_panel, 3); self.main_layout.addWidget(actions_panel, 1)
        self.load_themes_from_library()

    def show_creator_ui(self, theme_data=None):
        """Dynamically creates and displays the Theme Creator UI."""
        # Clear existing manager actions
        for action in self.manager_actions:
            self.removeAction(action)
            action.deleteLater()
        self.manager_actions = []

        while self.main_layout.count():
            widget = self.main_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            del widget
        self.runOnce = True
        self.asset_widgets = {}
        left_panel = QFrame()
        left_panel.setFrameShape(QFrame.Shape.StyledPanel); left_panel.setProperty("role", "panel"); left_panel.setMinimumWidth(600); left_layout = QVBoxLayout()
        self.btn_close_creator = QPushButton("<< Back to Manager"); self.btn_close_creator.clicked.connect(self.show_manager_ui); left_layout.addWidget(self.btn_close_creator)
        left_layout.addWidget(QLabel("<b>Asset Importer (Required: *)</b>"))
        asset_tabs = QTabWidget(); left_layout.addWidget(asset_tabs)
        def create_asset_row(name_id, placeholder, file_type="Image"):
            line_edit = QLineEdit(); line_edit.setPlaceholderText(placeholder); btn_browse = QPushButton("Browse..."); btn_browse.clicked.connect(lambda: self.browse_asset_file(line_edit, file_type)); layout = QHBoxLayout(); layout.addWidget(line_edit); layout.addWidget(btn_browse); self.asset_widgets[name_id] = line_edit; return layout
        def create_numeric_row(name_id, default_value=""):
            line_edit = QLineEdit(default_value); line_edit.setValidator(QIntValidator(0, 999999999)); self.asset_widgets[name_id] = line_edit; return line_edit
        main_tab = QWidget(); main_layout = QFormLayout()
        main_layout.addRow("Lock Screen *:", create_asset_row('lockpaper.png', '960x512 (auto-resized)'))
        main_layout.addRow("Home Page 1 *:", create_asset_row('br1.png', '960x512 (auto-resized)'))
        main_layout.addRow("Livearea Preview *:", create_asset_row('preview_livearea.png', '480x272 (auto-resized)'))
        main_layout.addRow("Lockscreen Preview *:", create_asset_row('preview_lockscreen.png', '480x272 (auto-resized)'))
        main_layout.addRow("Thumbnail Preview *:", create_asset_row('preview_thumbnail.png', '226x128 (auto-resized)'))
        if find_at9tool():
            main_layout.addRow("Background Music:", create_asset_row('bgm.at9', f"Optional {', '.join(ENCODABLE_AUDIO_EXTS)} or .at9", file_type="Audio"))
        else:
            main_layout.addRow("Background Music:", create_asset_row('bgm.at9', f"Optional .at9 ({', '.join(ENCODABLE_AUDIO_EXTS)} requires at9tool.exe)", file_type="Audio"))
            at9tool_notice = QPushButton("⚠ Audio encoding disabled (details)"); at9tool_notice.setFlat(True); at9tool_notice.setCursor(Qt.CursorShape.PointingHandCursor)
            at9tool_notice.setStyleSheet("color: grey; font-size: 10px; text-align: left; border: none;")
            at9tool_notice.clicked.connect(lambda: self.show_info_popup(at9tool_notice, AT9TOOL_REQUIRED_MSG))
            main_layout.addRow("", at9tool_notice)
        self.bgm_loop_label = QLabel("BGM Loop Start Sample:")
        main_layout.addRow(self.bgm_loop_label, create_numeric_row('m_bgmStartSample')); main_tab.setLayout(main_layout); self.asset_widgets['bgm.at9'].textChanged.connect(self.toggle_bgm_loop_input)
        pages_tab_widget = QWidget(); pages_scroll = QScrollArea(); pages_scroll.setWidgetResizable(True); pages_layout = QFormLayout(); pages_tab_widget.setLayout(pages_layout)
        for i in range(2, 11):
            page_layout = QHBoxLayout(); page_asset_row_le = QLineEdit(); page_asset_row_le.setPlaceholderText('Optional 960x512'); self.asset_widgets[f'br{i}.png'] = page_asset_row_le; page_asset_row_btn = QPushButton("Browse..."); page_asset_row_btn.clicked.connect(lambda ch, le=page_asset_row_le: self.browse_asset_file(le, "Image")); page_asset_row_layout = QHBoxLayout(); page_asset_row_layout.addWidget(page_asset_row_le); page_asset_row_layout.addWidget(page_asset_row_btn); page_layout.addLayout(page_asset_row_layout); wave_combo = QComboBox(); wave_combo.addItems([str(n) for n in range(32)]); wave_combo.setCurrentText("12"); wave_combo.setEnabled(False);
            longest = max([str(n) for n in range(32)], key=len)
            tWidth = wave_combo.fontMetrics().boundingRect(longest).width()
            wave_combo.view().setMinimumWidth(tWidth + 25)
            self.asset_widgets[f'br{i}_wave'] = wave_combo; color_picker = ColorPickerButton(); color_picker.setEnabled(False); self.asset_widgets[f'br{i}_color'] = color_picker

            # Connect previous image to enable current page controls and next image input
            prev_img_widget = self.asset_widgets[f'br{i-1}.png']
            curr_img_widget = self.asset_widgets[f'br{i}.png']
            next_img_widget = self.asset_widgets.get(f'br{i+1}.png') # May be None for page 10
            prev_img_widget.textChanged.connect(lambda text, w=wave_combo, c=color_picker, le_curr=curr_img_widget, le_next=next_img_widget: self.update_page_controls(text, le_curr, w, c, le_next))
            page_layout.addWidget(QLabel("Wave:")); page_layout.addWidget(wave_combo); page_layout.addWidget(QLabel("Color:")); page_layout.addWidget(color_picker); pages_layout.addRow(f"Home Page {i}:", page_layout)
        pages_scroll.setWidget(pages_tab_widget)
        icons_tab_widget = QWidget(); icons_scroll = QScrollArea(); icons_scroll.setWidgetResizable(True); icons_layout = QFormLayout(); icons_tab_widget.setLayout(icons_layout); icon_list = ['icon_web.png', 'icon_calendar.png', 'icon_photos.png', 'icon_email.png', 'icon_friend.png', 'icon_cma.png', 'icon_message.png', 'icon_music.png', 'icon_near.png', 'icon_parental.png', 'icon_party.png', 'icon_power.png', 'icon_ps3link.png', 'icon_ps4link.png', 'icon_settings.png', 'icon_trophy.png', 'icon_video.png'];
        if ICON_AI_AVAILABLE:
            btn_generate_icons = QPushButton("✨ Generate Icons with AI..."); btn_generate_icons.setToolTip("Restyle the template icons to match this theme's wallpapers (optional download)")
            btn_generate_icons.clicked.connect(self.open_icon_generator); icons_layout.addRow(btn_generate_icons)
        for icon in icon_list: icons_layout.addRow(f"{icon}:", create_asset_row(icon, 'Optional 128x128'))
        icons_scroll.setWidget(icons_tab_widget)
        ui_tab = QWidget(); ui_layout = QFormLayout(); ui_layout.addRow("Page Indicator (other):", create_asset_row('screen_other.png', 'Optional 22x22')); ui_layout.addRow("Current Page (current):", create_asset_row('screen_current.png', 'Optional 22x22')); ui_layout.addRow("No Notice:", create_asset_row('noNotice.png', 'Optional 120x110')); ui_layout.addRow("New Notice:", create_asset_row('newNotice.png', 'Optional 120x110')); ui_tab.setLayout(ui_layout)
        colors_tab_widget = QWidget(); colors_scroll = QScrollArea(); colors_scroll.setWidgetResizable(True); colors_layout = QFormLayout(); colors_tab_widget.setLayout(colors_layout)
        def create_color_row(name_id, label_text, default_color): color_picker = ColorPickerButton(default_color); self.asset_widgets[name_id] = color_picker; colors_layout.addRow(label_text, color_picker)
        colors_layout.addRow(QLabel("<b>Start Screen</b>")); create_color_row('m_dateColor', 'Date Color:', '#ffffffff'); create_color_row('m_notifyBgColor', 'Notify BG Color:', '#ffffffff'); create_color_row('m_notifyBorderColor', 'Notify Border Color:', '#ffcccccc'); create_color_row('m_notifyFontColor', 'Notify Font Color:', '#ffffffff'); colors_layout.addRow(QLabel("<b>Info Bar</b>")); create_color_row('m_barColor', 'Bar Color:', '#ff000000'); create_color_row('m_indicatorColor', 'Indicator Color:', '#ffffffff'); create_color_row('m_noticeFontColor', 'Notice Font Color:', '#ffffffff'); create_color_row('m_noticeGlowColor', 'Notice Glow Color:', '#ffca0000'); colors_layout.addRow(QLabel("<b>Home Screen</b>"))
        self.use_global_color_checkbox = QCheckBox("Use one font color for all pages"); self.use_global_color_checkbox.setChecked(True); self.asset_widgets['use_global_font_color'] = self.use_global_color_checkbox; self.use_global_color_checkbox.toggled.connect(self.toggle_per_page_colors); colors_layout.addRow(self.use_global_color_checkbox); create_color_row('m_fontColor', 'Global Font Color:', '#ffffffff'); colors_scroll.setWidget(colors_tab_widget)
        asset_tabs.addTab(main_tab, "Main"); asset_tabs.addTab(pages_scroll, "Home Pages (2-10)"); asset_tabs.addTab(icons_scroll, "System Icons"); asset_tabs.addTab(ui_tab, "UI Images"); asset_tabs.addTab(colors_scroll, "Colors"); left_panel.setLayout(left_layout)
        center_panel = QFrame()
        center_panel.setFrameShape(QFrame.Shape.StyledPanel); center_panel.setProperty("role", "panel"); center_panel.setMinimumWidth(400); center_panel.setMaximumHeight(700)
        self.preview_label1 = QLabel("Livearea Preview"); self.preview_label1.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview_label1.setProperty("role", "preview"); self.preview_label1.setStyleSheet("font-size: 24px;")
        self.preview_label1.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        center_layout = QVBoxLayout(); center_layout.addWidget(self.preview_label1)
        self.preview_label2 = QLabel("Lockscreen Preview"); self.preview_label2.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview_label2.setProperty("role", "preview"); self.preview_label2.setStyleSheet("font-size: 24px;")
        self.preview_label2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        center_layout.addWidget(self.preview_label2);
        self.preview_label3 = QLabel("Thumbnail Preview"); self.preview_label3.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview_label3.setProperty("role", "preview"); self.preview_label3.setStyleSheet("font-size: 24px;")
        self.preview_label3.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        center_layout.addWidget(self.preview_label3);
        center_panel.setLayout(center_layout)

        self.asset_widgets['preview_livearea.png'].textChanged.connect(lambda e:self.update_preview_image(e, self.preview_label1, 'Livearea Preview'))
        self.asset_widgets['preview_lockscreen.png'].textChanged.connect(lambda e:self.update_preview_image(e, self.preview_label2, 'Lockscreen Preview'))
        self.asset_widgets['preview_thumbnail.png'].textChanged.connect(lambda e:self.update_preview_image(e, self.preview_label3, 'Thumbnail Preview'))

        right_panel = QFrame()
        right_panel.setFrameShape(QFrame.Shape.StyledPanel); right_panel.setProperty("role", "panel");right_panel.setMinimumWidth(200); right_layout = QVBoxLayout(); right_layout.addWidget(QLabel("<b>Theme Properties</b>")); right_layout.addWidget(QLabel("Theme Title: * (m_title)")); self.title_input = QLineEdit(); self.asset_widgets['m_title'] = self.title_input; right_layout.addWidget(self.title_input); right_layout.addWidget(QLabel("Author: (m_provider)")); self.author_input = QLineEdit(); self.asset_widgets['m_author'] = self.author_input; right_layout.addWidget(self.author_input); right_layout.addWidget(QLabel("Version: * (m_contentVer)")); self.version_input = QLineEdit("01.00"); self.version_input.setPlaceholderText("Format: 01.00"); self.asset_widgets['m_version'] = self.version_input; right_layout.addWidget(self.version_input); right_layout.addStretch(); build_button = QPushButton("Build/Save Theme"); build_button.setProperty("role", "success"); build_button.clicked.connect(self.build_theme); right_layout.addWidget(build_button); right_panel.setLayout(right_layout)

        if theme_data: self.load_theme_data_into_creator(theme_data)
        else: self.title_input.setText("My New Theme"); self.apply_settings_to_creator(); self.current_theme_path = None; self.toggle_bgm_loop_input(self.asset_widgets['bgm.at9'].text())

        self.toggle_per_page_colors(self.use_global_color_checkbox.isChecked())
        for i in range(1, 10): # Trigger initial cascade enable/disable for pages 2-10
            line_edit = self.asset_widgets.get(f'br{i}.png')
            curr_line_edit = self.asset_widgets.get(f'br{i+1}.png')
            wave_combo = self.asset_widgets.get(f'br{i+1}_wave')
            color_picker = self.asset_widgets.get(f'br{i+1}_color')
            next_line_edit = self.asset_widgets.get(f'br{i+2}.png')
            if line_edit and curr_line_edit and wave_combo and color_picker:
                self.update_page_controls(line_edit.text(), curr_line_edit, wave_combo, color_picker, next_line_edit)

        # Adjusted layout ratios: Asset Importer gets more space (3 vs 2 vs 1)

        self.main_layout.addWidget(left_panel, stretch=3); self.main_layout.addWidget(center_panel, stretch=2); self.main_layout.addWidget(right_panel, stretch=1)

    # --- UPDATED: Load theme data correctly ---
    def load_theme_data_into_creator(self, theme_data):
        """Populates the creator UI fields when editing a theme."""
        self.title_input.setText(theme_data.get('title', ''))
        self.author_input.setText(theme_data.get('author', ''))
        self.version_input.setText(theme_data.get('version', '01.00'))
        self.current_theme_path = theme_data.get('path')

        for asset_id, path in theme_data.get('assets', {}).items():
            widget = self.asset_widgets.get(asset_id)
            if widget and isinstance(widget, QLineEdit):
                full_path = os.path.join(self.current_theme_path, path)
                if os.path.exists(full_path): widget.setText(full_path)

        self.asset_widgets['m_bgmStartSample'].setText(str(theme_data.get('bgm_loop_start', '0')))
        self.toggle_bgm_loop_input(self.asset_widgets['bgm.at9'].text())

        for color_id, hex_val in theme_data.get('colors', {}).items():
            widget = self.asset_widgets.get(color_id)
            if widget and isinstance(widget, ColorPickerButton): widget.color_hex = hex_val; widget.update_color()

        # Load per-page colors and waves, determine if global checkbox should be checked
        all_colors_same = True
        first_color = theme_data.get('page_colors', {}).get('br1_color')
        if not first_color and 'm_fontColor' in self.asset_widgets: # Use global if page 1 color missing
            first_color = self.asset_widgets['m_fontColor'].get_color()
        if not first_color: first_color = '#ffffffff' # Absolute fallback

        for i in range(1, 11):
            wave_widget = self.asset_widgets.get(f'br{i}_wave') if i > 1 else None
            color_widget = self.asset_widgets.get(f'br{i}_color') if i > 1 else self.asset_widgets.get('m_fontColor')

            wave_val = theme_data.get('page_waves', {}).get(f'br{i}_wave', '12')
            color_val = theme_data.get('page_colors', {}).get(f'br{i}_color', first_color)

            if wave_widget: wave_widget.setCurrentText(wave_val)
            if color_widget:
                color_widget.color_hex = color_val
                color_widget.update_color()
                if color_val != first_color: # Check against the color of page 1
                    all_colors_same = False
        QTimer.singleShot(5, lambda: self.update_preview_image(self.asset_widgets['preview_livearea.png'].text(), self.preview_label1, 'Livearea Preview'))
        QTimer.singleShot(5, lambda: self.update_preview_image(self.asset_widgets['preview_lockscreen.png'].text(), self.preview_label2, 'Lockscreen Preview'))
        QTimer.singleShot(5, lambda: self.update_preview_image(self.asset_widgets['preview_thumbnail.png'].text(), self.preview_label3, 'Thumbnail Preview'))
        self.asset_widgets['use_global_font_color'].setChecked(all_colors_same)
    # --- UPDATED: Enable next page input as well ---
    def update_page_controls(self, prev_image_text, current_image_lineedit, current_wave_widget, current_color_widget, next_image_lineedit=None):
        """Enable/disable per-page controls and the *next* page's image input."""
        current_controls_enabled = bool(prev_image_text)
        current_wave_widget.setEnabled(current_controls_enabled)

        if not self.asset_widgets['use_global_font_color'].isChecked():
            current_color_widget.setEnabled(current_controls_enabled)
        else:
             current_color_widget.setEnabled(False)

        if next_image_lineedit:
            next_browse_button = None
            # Find the browse button associated with the next line edit
            layout = next_image_lineedit.parentWidget().layout() # Get the QHBoxLayout
            if layout:
                 # Iterate through items in the layout to find the button
                 for i in range(layout.count()):
                      widget_item = layout.itemAt(i).widget()
                      if isinstance(widget_item, QPushButton) and widget_item.text() == "Browse...":
                           next_browse_button = widget_item
                           break

            # Enable next line edit only if the *current* page has an image specified
            next_controls_enabled = bool(current_image_lineedit.text())
            next_image_lineedit.setEnabled(next_controls_enabled)
            if next_browse_button:
                next_browse_button.setEnabled(next_controls_enabled)

            # If disabling next line edit, clear its text to cascade disabling
            if not next_controls_enabled:
                 next_image_lineedit.clear()


    # --- UPDATED: Handle enabling/disabling the NEXT page input ---
    def toggle_per_page_colors(self, use_global):
        """Enable/disable all per-page color pickers based on the global checkbox."""
        self.asset_widgets['m_fontColor'].setEnabled(use_global)

        for i in range(2, 11):
            color_widget = self.asset_widgets.get(f'br{i}_color')
            image_widget = self.asset_widgets.get(f'br{i}.png')
            if color_widget and image_widget:
                color_widget.setEnabled(not use_global and bool(image_widget.text()))

    # --- UPDATED: Show BGM loop input only for sources that get encoded ---
    def bgm_needs_encoding(self, bgm_path_text):
        """True for any non-.at9 BGM source (the loop point is set while encoding to AT9)."""
        path = bgm_path_text.strip().lower()
        return bool(path) and not path.endswith('.at9')

    def toggle_bgm_loop_input(self, bgm_path_text=""):
        """Shows the BGM loop start input only when the BGM will be encoded (i.e. is not already .at9)."""
        needs_encoding = self.bgm_needs_encoding(bgm_path_text)
        widget = self.asset_widgets.get('m_bgmStartSample')
        if widget:
            widget.setVisible(needs_encoding); widget.setEnabled(needs_encoding)
            self.bgm_loop_label.setVisible(needs_encoding)

    # --- UPDATED: Update preview image handling ---
    def update_preview_image(self, path, label, text):
        """Updates the central preview panel with the selected image."""
        if path and os.path.exists(path):
            try:
                pixmap = QPixmap(path);
                if not pixmap.isNull():
                    tw = label.width()
                    th = round(tw * 17 / 30)
                    label.setPixmap(pixmap.scaled(QSize(tw, th), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
                else:
                    label.setText("Preview (Invalid Image)"); label.setPixmap(QPixmap())
            except Exception as e: print(f"Error loading preview image {path}: {e}"); label.setText("Preview (Error)"); label.setPixmap(QPixmap())
        else: label.setText(text); label.setPixmap(QPixmap())
        if  not self.preview_label1.pixmap().isNull() and not self.preview_label2.pixmap().isNull() and not self.preview_label3.pixmap().isNull() and self.runOnce:
            self.runOnce = False
            if self.isMaximized():
                self.showNormal()
                QTimer.singleShot(20, self.showMaximized)


    def show_info_popup(self, anchor, text):
        """Shows a context-menu-style popup with wrapped text below the anchor widget; closes on outside click."""
        popup = QFrame(self, Qt.WindowType.Popup); popup.setFrameShape(QFrame.Shape.StyledPanel); popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(popup); label = QLabel(text); label.setWordWrap(True); label.setFixedWidth(340); layout.addWidget(label)
        popup.adjustSize(); popup.move(anchor.mapToGlobal(QPoint(0, anchor.height()))); popup.show()

    def open_icon_generator(self):
        """Opens the AI icon generator, using the theme's wallpapers as style references."""
        wallpaper_ids = ['lockpaper.png'] + [f'br{i}.png' for i in range(1, 11)]
        wallpapers = [p for p in (self.asset_widgets[w].text() for w in wallpaper_ids) if p and os.path.isfile(p)]
        # Themes often reuse one picture on several pages: each counts once.
        unique = {}
        for p in wallpapers: unique.setdefault(os.path.normcase(os.path.abspath(p)), p)
        wallpapers = list(unique.values())
        # The theme's own colours (Colors tab) so the icons match the theme.
        theme_colors = [w.get_color() for w in self.asset_widgets.values() if isinstance(w, ColorPickerButton)]
        dialog = IconGeneratorDialog(self, self.title_input.text(), wallpapers, theme_colors)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            for icon_name, path in dialog.generated.items():
                widget = self.asset_widgets.get(icon_name)
                if widget: widget.setText(path)
            # The editor now owns these temporary icons until it is left (saved
            # or not); earlier generated sets the slots no longer use go now.
            self.icon_gen_dirs.append(dialog.output_dir)
            in_use = {os.path.dirname(w.text()) for w in self.asset_widgets.values() if isinstance(w, QLineEdit) and w.text()}
            for folder in [d for d in self.icon_gen_dirs if d not in in_use]:
                shutil.rmtree(folder, ignore_errors=True)
                self.icon_gen_dirs.remove(folder)

    def clear_generated_icons(self):
        """Delete the temporary AI-generated icons: called when the theme editor
        is left (after saving they have already been copied into the theme)."""
        for folder in getattr(self, 'icon_gen_dirs', []):
            shutil.rmtree(folder, ignore_errors=True)
        self.icon_gen_dirs = []

    def browse_asset_file(self, line_edit, file_type):
        if file_type == "Image": filter = "Images (*.png *.jpg *.jpeg)"
        elif file_type == "Audio":
            # Only offer encodable formats when at9tool is available to encode them
            exts = (ENCODABLE_AUDIO_EXTS + ['.at9']) if find_at9tool() else ['.at9']
            filter = f"Audio ({' '.join(f'*{e} *{e.upper()}' for e in exts)})"
        else: filter = "All Files (*.*)"
        path, _ = QFileDialog.getOpenFileName(self, f"Select {file_type} File", "", filter);
        # Fallback: other formats can still be typed into the dialog's filename box
        if path and file_type == "Audio" and self.bgm_needs_encoding(path) and not find_at9tool():
            QMessageBox.information(self, "at9tool.exe Required", AT9TOOL_REQUIRED_MSG); return
        if path: line_edit.setText(path)

    # -------------------------------------------------------------------------
    # Core Theme Management Logic (XML, Folders, Zip)
    # -------------------------------------------------------------------------
    # --- UPDATED: parse_theme_xml includes previews and per-page data ---
    def parse_theme_xml(self, xml_path):
        """Reads the official theme.xml file and returns a full data dict."""
        try:
            tree = ET.parse(xml_path); root = tree.getroot()
            data = {}
            data['title'] = root.findtext('./InfomationProperty/m_title/m_default')
            data['author'] = root.findtext('./InfomationProperty/m_provider/m_default')
            data['version'] = root.findtext('./InfomationProperty/m_contentVer')
            data['assets'] = {}
            asset_tags_to_find = [
                ('lockpaper.png', './StartScreenProperty/m_filePath'), ('bgm.at9', './HomeProperty/m_bgmFilePath'),
                ('preview_livearea.png', './InfomationProperty/m_homePreviewFilePath'), ('preview_lockscreen.png', './InfomationProperty/m_startPreviewFilePath'), ('preview_thumbnail.png', './InfomationProperty/m_packageImageFilePath'),
                ('br1.png', './HomeProperty/m_bgParam/BackgroundParam[1]/m_imageFilePath'), ('br2.png', './HomeProperty/m_bgParam/BackgroundParam[2]/m_imageFilePath'), ('br3.png', './HomeProperty/m_bgParam/BackgroundParam[3]/m_imageFilePath'), ('br4.png', './HomeProperty/m_bgParam/BackgroundParam[4]/m_imageFilePath'), ('br5.png', './HomeProperty/m_bgParam/BackgroundParam[5]/m_imageFilePath'), ('br6.png', './HomeProperty/m_bgParam/BackgroundParam[6]/m_imageFilePath'), ('br7.png', './HomeProperty/m_bgParam/BackgroundParam[7]/m_imageFilePath'), ('br8.png', './HomeProperty/m_bgParam/BackgroundParam[8]/m_imageFilePath'), ('br9.png', './HomeProperty/m_bgParam/BackgroundParam[9]/m_imageFilePath'), ('br10.png', './HomeProperty/m_bgParam/BackgroundParam[10]/m_imageFilePath'),
                ('icon_web.png', './HomeProperty/m_browser/m_iconFilePath'), ('icon_calendar.png', './HomeProperty/m_calendar/m_iconFilePath'), ('icon_photos.png', './HomeProperty/m_camera/m_iconFilePath'), ('icon_email.png', './HomeProperty/m_email/m_iconFilePath'), ('icon_friend.png', './HomeProperty/m_friend/m_iconFilePath'), ('icon_cma.png', './HomeProperty/m_hostCollabo/m_iconFilePath'), ('icon_message.png', './HomeProperty/m_message/m_iconFilePath'), ('icon_music.png', './HomeProperty/m_music/m_iconFilePath'), ('icon_near.png', './HomeProperty/m_near/m_iconFilePath'), ('icon_parental.png', './HomeProperty/m_parental/m_iconFilePath'), ('icon_party.png', './HomeProperty/m_party/m_iconFilePath'), ('icon_power.png', './HomeProperty/m_power/m_iconFilePath'), ('icon_ps3link.png', './HomeProperty/m_ps3Link/m_iconFilePath'), ('icon_ps4link.png', './HomeProperty/m_ps4Link/m_iconFilePath'), ('icon_settings.png', './HomeProperty/m_settings/m_iconFilePath'), ('icon_trophy.png', './HomeProperty/m_trophy/m_iconFilePath'), ('icon_video.png', './HomeProperty/m_video/m_iconFilePath'),
                ('screen_other.png', './HomeProperty/m_basePageFilePath'), ('screen_current.png', './HomeProperty/m_curPageFilePath'),
                ('noNotice.png', './InfomationBarProperty/m_noNoticeFilePath'), ('newNotice.png', './InfomationBarProperty/m_newNoticeFilePath'),
            ]
            for name, path in asset_tags_to_find:
                found_path = root.findtext(path);
                if found_path:
                    data['assets'][name] = found_path
            data['colors'] = {}
            color_tags = {'m_dateColor': './StartScreenProperty/m_dateColor', 'm_notifyBgColor': './StartScreenProperty/m_notifyBgColor', 'm_notifyBorderColor': './StartScreenProperty/m_notifyBorderColor', 'm_notifyFontColor': './StartScreenProperty/m_notifyFontColor', 'm_barColor': './InfomationBarProperty/m_barColor', 'm_indicatorColor': './InfomationBarProperty/m_indicatorColor', 'm_noticeFontColor': './InfomationBarProperty/m_noticeFontColor', 'm_noticeGlowColor': './InfomationBarProperty/m_noticeGlowColor'}
            for name, path in color_tags.items():
                found_color = root.findtext(path);
                if found_color:
                    if len(found_color.lstrip('#')) == 6: found_color = f"ff{found_color.lstrip('#')}"
                    data['colors'][name] = found_color
            data['page_colors'] = {}
            data['page_waves'] = {}
            for i in range(1, 11):
                 page_color = root.findtext(f'./HomeProperty/m_bgParam/BackgroundParam[{i}]/m_fontColor')
                 page_wave = root.findtext(f'./HomeProperty/m_bgParam/BackgroundParam[{i}]/m_waveType')
                 if page_color:
                     if len(page_color.lstrip('#')) == 6: page_color = f"ff{page_color.lstrip('#')}"
                     data['page_colors'][f'br{i}_color'] = page_color
                 if page_wave: data['page_waves'][f'br{i}_wave'] = page_wave
            return data
        except (FileNotFoundError, ET.ParseError) as e: print(f"Error parsing {xml_path}: {e}"); return {'title': 'Invalid Theme', 'author': 'N/A', 'version': '00.00', 'assets': {}, 'colors': {}, 'page_colors': {}, 'page_waves': {}, 'bgm_loop_start':'0'}

    # --- UPDATED: write_theme_xml includes per-page data and conditional writing ---
    def write_theme_xml(self, theme_folder_path, theme_data):
        """Creates the full, official theme.xml file, only including optional elements if provided."""
        try:
            def create_sub_element(parent, name, text=None):
                el = ET.SubElement(parent, name);
                if text:
                    el.text = text
                return el
            def create_localized_text(parent, name, default_text):
                el = create_sub_element(parent, name); create_sub_element(el, 'm_default', default_text);
                param = create_sub_element(el, 'm_param');
                langs = ['m_da', 'm_de', 'm_es', 'm_fi', 'm_fr', 'm_it', 'm_nl', 'm_no', 'm_pl', 'm_pt', 'm_sv', 'm_ja'];
                for lang in langs:
                    create_sub_element(param, lang, default_text)
            title = theme_data.get('title', 'Untitled'); author = theme_data.get('author', 'Unknown'); version = theme_data.get('version', '01.00'); colors = theme_data.get('colors', {}); assets = theme_data.get('assets', {}); page_waves = theme_data.get('page_waves', {}); page_colors = theme_data.get('page_colors', {})
            root = ET.Element('theme', attrib={'format-ver': '01.00', 'package': '0'})
            home = create_sub_element(root, 'HomeProperty')
            bg_param = create_sub_element(home, 'm_bgParam')
            br1_path = assets.get('br1.png', 'br1.png'); tbr1_path = assets.get('tbr1.png', 'tbr1.png'); global_font_color = colors.get('m_fontColor', 'ffffffff'); use_global_color = theme_data.get('use_global_font_color', True)
            for i in range(1, 11):
                br_asset_id = f'br{i}.png'; tbr_asset_id = f'tbr{i}.png'; br_path = assets.get(br_asset_id); tbr_path = assets.get(tbr_asset_id)
                if br_path: # Only create BackgroundParam if the image file exists in assets
                    tbr_path = tbr_path if tbr_path else tbr1_path
                    font_color = global_font_color if use_global_color else page_colors.get(f'br{i}_color', global_font_color)
                    wave_type = page_waves.get(f'br{i}_wave', '12')
                    bg = create_sub_element(bg_param, 'BackgroundParam')
                    create_sub_element(bg, 'm_imageFilePath', br_path)
                    create_sub_element(bg, 'm_thumbnailFilePath', tbr_path)
                    create_sub_element(bg, 'm_waveType', wave_type)
                    create_sub_element(bg, 'm_fontColor', font_color)
                    create_sub_element(bg, 'm_fontShadow', '1')
                elif i == 1: # Always ensure page 1 exists
                    bg = create_sub_element(bg_param, 'BackgroundParam'); create_sub_element(bg, 'm_imageFilePath', br1_path); create_sub_element(bg, 'm_thumbnailFilePath', tbr1_path); create_sub_element(bg, 'm_waveType', '12'); create_sub_element(bg, 'm_fontColor', global_font_color); create_sub_element(bg, 'm_fontShadow', '1')
            bgm_path = assets.get('bgm.at9');
            if bgm_path: create_sub_element(home, 'm_bgmFilePath', bgm_path) # BGM path ONLY, no loop info
            icons = {'m_browser': 'icon_web.png', 'm_calendar': 'icon_calendar.png', 'm_camera': 'icon_photos.png', 'm_email': 'icon_email.png', 'm_friend': 'icon_friend.png', 'm_hostCollabo': 'icon_cma.png', 'm_message': 'icon_message.png', 'm_music': 'icon_music.png', 'm_near': 'icon_near.png', 'm_parental': 'icon_parental.png', 'm_party': 'icon_party.png', 'm_power': 'icon_power.png', 'm_ps3Link': 'icon_ps3link.png', 'm_ps4Link': 'icon_ps4link.png', 'm_settings': 'icon_settings.png', 'm_trophy': 'icon_trophy.png', 'm_video': 'icon_video.png'}
            for tag, icon_file in icons.items():
                if assets.get(icon_file): icon_el = create_sub_element(home, tag); create_sub_element(icon_el, 'm_iconFilePath', assets.get(icon_file))
            if assets.get('screen_other.png'): create_sub_element(home, 'm_basePageFilePath', assets.get('screen_other.png'))
            if assets.get('screen_current.png'): create_sub_element(home, 'm_curPageFilePath', assets.get('screen_current.png'))
            info_bar = create_sub_element(root, 'InfomationBarProperty'); create_sub_element(info_bar, 'm_barColor', colors.get('m_barColor', 'ff000000')); create_sub_element(info_bar, 'm_indicatorColor', colors.get('m_indicatorColor', 'ffffffff')); create_sub_element(info_bar, 'm_noticeFontColor', colors.get('m_noticeFontColor', 'ffffffff')); create_sub_element(info_bar, 'm_noticeGlowColor', colors.get('m_noticeGlowColor', 'ffca0000'))
            if assets.get('noNotice.png'): create_sub_element(info_bar, 'm_noNoticeFilePath', assets.get('noNotice.png'))
            if assets.get('newNotice.png'): create_sub_element(info_bar, 'm_newNoticeFilePath', assets.get('newNotice.png'))
            info_prop = create_sub_element(root, 'InfomationProperty'); create_localized_text(info_prop, 'm_provider', author); create_sub_element(info_prop, 'm_contentVer', version); create_localized_text(info_prop, 'm_title', title); create_sub_element(info_prop, 'm_homePreviewFilePath', assets.get('preview_livearea.png','preview_livearea.png')); create_sub_element(info_prop, 'm_startPreviewFilePath', assets.get('preview_lockscreen.png', 'preview_lockscreen.png')); create_sub_element(info_prop, 'm_packageImageFilePath', assets.get('preview_thumbnail.png', 'preview_thumbnail.png'))
            start_screen = create_sub_element(root, 'StartScreenProperty'); create_sub_element(start_screen, 'm_dateColor', colors.get('m_dateColor', 'ffffffff')); create_sub_element(start_screen, 'm_dateLayout', '0'); create_sub_element(start_screen, 'm_filePath', assets.get('lockpaper.png', 'lockpaper.png')); create_sub_element(start_screen, 'm_notifyBgColor', colors.get('m_notifyBgColor', 'ffffffff')); create_sub_element(start_screen, 'm_notifyBorderColor', colors.get('m_notifyBorderColor', 'ffcccccc')); create_sub_element(start_screen, 'm_notifyFontColor', colors.get('m_notifyFontColor', 'ffffffff'))
            xml_str = ET.tostring(root, 'utf-8'); final_xml = '<?xml version="1.0" encoding="utf-8"?>\n' + xml_str.decode('utf-8')
            final_xml = minidom.parseString(final_xml).toprettyxml(indent="\t", encoding="utf-8").decode("utf-8")
            xml_path = os.path.join(theme_folder_path, 'theme.xml');
            with open(xml_path, 'w', encoding='utf-8') as f: f.write(final_xml); return True
        except Exception as e:
            QMessageBox.critical(self, "XML Error", f"Could not write theme.xml: {e}"); return False

    def validate_assets_on_build(self):
        """Checks assets before building. Requires file existence and valid version."""
        validation_errors = []; version_text = self.asset_widgets.get('m_version').text();
        if not re.match(r'^\d{2}\.\d{2}$', version_text): validation_errors.append(f"Version must be in the '01.00' format.")
        def check_exists(asset_id, required, file_types=None):
            widget = self.asset_widgets.get(asset_id); path = "";
            if isinstance(widget, QLineEdit): path = widget.text()
            else: return True
            if not path:
                if required: validation_errors.append(f"{asset_id} is required.")
                return False
            if not os.path.exists(path): validation_errors.append(f"{asset_id} file not found:\n{path}"); return False
            if file_types and not any(path.lower().endswith(ft) for ft in file_types): validation_errors.append(f"{asset_id} must be one of: {', '.join(file_types)}"); return False
            return True
        if not self.asset_widgets.get('m_title').text(): validation_errors.append("Theme Title cannot be empty.")
        check_exists('lockpaper.png', required=True); check_exists('br1.png', required=True); check_exists('preview_livearea.png', required=True); check_exists('preview_lockscreen.png', required=True); check_exists('preview_thumbnail.png', required=True)
        if check_exists('bgm.at9', required=False, file_types=ENCODABLE_AUDIO_EXTS + ['.at9']) and self.bgm_needs_encoding(self.asset_widgets['bgm.at9'].text()) and not find_at9tool():
            validation_errors.append(AT9TOOL_REQUIRED_MSG)
        for asset_id, widget in self.asset_widgets.items():
             if asset_id not in ['lockpaper.png', 'br1.png', 'preview_livearea.png', 'preview_lockscreen.png', 'preview_thumbnail.png', 'bgm.at9', 'm_title', 'm_author', 'm_version', 'm_bgmStartSample'] and isinstance(widget, QLineEdit) and widget.text(): check_exists(asset_id, required=False)
        if validation_errors: return False, "\n\n".join(validation_errors)
        return True, "All assets are valid."

    def find_theme_problem(self, theme_path, theme_data):
        """Returns (error_title, error_message) if the theme is invalid, else None."""
        theme_name = os.path.basename(theme_path)
        requiredVars = [theme_data.get('title'),
                        theme_data.get('version'),
                        theme_data.get('author')]
        required = [theme_data['assets'].get('preview_thumbnail.png'),
                    theme_data['assets'].get('preview_livearea.png'),
                    theme_data['assets'].get('preview_lockscreen.png'),
                    theme_data['assets'].get('br1.png'),
                    theme_data['assets'].get('lockpaper.png')]
        if None in required or None in requiredVars:
            return "Theme XML Error", f"The xml file in {theme_name} is invalid."
        missing = []
        for file in required:
            if not os.path.exists(os.path.join(theme_path, file)):
                missing.append(file)
        if missing:
            formatedMissing = '\n '.join(missing)
            return "Theme Required Assets Error", f"Assets in theme xml for {theme_name} are missing:\n{formatedMissing}"
        for key in theme_data['assets'].keys():
            if theme_data['assets'].get(key) != None:
                if not os.path.exists(os.path.join(theme_path, theme_data['assets'].get(key))):
                    missing.append(theme_data['assets'].get(key))
        if missing:
            formatedMissing = '\n '.join(missing)
            return "Theme Assets Error", f"Assets in theme xml for {theme_name} are missing:\n{formatedMissing}"
        return None

    def checkRequirements(self, theme_path, theme_data):
        problem = self.find_theme_problem(theme_path, theme_data)
        if problem:
            QMessageBox.critical(self, *problem)
            return False
        return True

    def load_themes_from_library(self):
        """Scans the theme library folder, reads data, and populates the list."""
        self.theme_list.clear(); self.theme_items_cache = []
        for theme_name in os.listdir(self.theme_library_path):
            theme_path = os.path.join(self.theme_library_path, theme_name);
            if not os.path.isdir(theme_path): continue
            xml_path = os.path.join(theme_path, 'theme.xml'); theme_data = {'title': theme_name, 'author': 'N/A', 'version': '00.00', 'path': theme_path}; display_text = f"{theme_name} (No theme.xml)"; icon = QIcon()
            if os.path.exists(xml_path):
                parsed_data = self.parse_theme_xml(xml_path); theme_data.update(parsed_data)
                display_text = f"{theme_data.get('title', theme_name)} ({theme_data.get('version')})\nby {theme_data.get('author', 'Unknown')}"
                thumb_path = os.path.join(theme_path, f"{theme_data['assets'].get('preview_thumbnail.png')}")
                if not self.checkRequirements(theme_path, theme_data):
                    continue
                icon = QIcon(thumb_path)
                self.theme_items_cache.append({'display_text': display_text, 'path': theme_path, 'title_lower': theme_data['title'].lower(), 'author_lower': theme_data['author'].lower(), 'version': theme_data['version'], 'icon': icon})
        self.sort_and_display_local_themes() # Call the combined sort/display function

    def sort_and_display_local_themes(self):
        """Sorts the cached theme data and displays it in the list widget."""
        sort_mode = self.local_sort_combo.currentText()
        def version_sort_key(item):
             try: return [int(part) for part in item['version'].split('.')]
             except: return [0, 0]
        if sort_mode == "Alphabetical (A-Z)": self.theme_items_cache.sort(key=lambda x: x['title_lower'])
        elif sort_mode == "Alphabetical (Z-A)": self.theme_items_cache.sort(key=lambda x: x['title_lower'], reverse=True)
        elif sort_mode == "Author (A-Z)": self.theme_items_cache.sort(key=lambda x: x['author_lower'])
        elif sort_mode == "Author (Z-A)": self.theme_items_cache.sort(key=lambda x: x['author_lower'], reverse=True)
        elif sort_mode == "Version (High-Low)": self.theme_items_cache.sort(key=version_sort_key, reverse=True)
        elif sort_mode == "Version (Low-High)": self.theme_items_cache.sort(key=version_sort_key)
        self.theme_list.clear()
        for item_data in self.theme_items_cache:
            item = QListWidgetItem(item_data['display_text']); item.setData(Qt.ItemDataRole.UserRole, item_data['path']); item.setData(Qt.ItemDataRole.UserRole + 1, item_data['title_lower']); item.setData(Qt.ItemDataRole.UserRole + 2, item_data['author_lower']); item.setData(Qt.ItemDataRole.UserRole + 3, item_data['version']); item.setIcon(item_data['icon']); self.theme_list.addItem(item)
        self.filter_local_themes() # Apply current filter

    def sort_local_themes(self, index=0):
        """Triggered by the sort combobox."""
        self.sort_and_display_local_themes()

    def filter_local_themes(self):
        """Filters the local theme list based on the search bar."""
        search_text = self.local_search_bar.text().lower(); search_by = self.local_search_combo.currentText()
        for i in range(self.theme_list.count()):
            item = self.theme_list.item(i);
            if not item: continue
            target_data = "";
            if search_by == "Search by Title": target_data = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            elif search_by == "Search by Author": target_data = item.data(Qt.ItemDataRole.UserRole + 2) or ""
            item.setHidden(search_text not in target_data)

    def get_unique_theme_path(self, base_name):
        """Finds a unique folder name in the library, e.g., 'theme1'."""
        safe_name = 'theme'
        target_path = os.path.join(self.theme_library_path, safe_name);
        if not os.path.exists(target_path): return target_path
        counter = 1
        while True:
            new_name = f"{safe_name}{counter}"; new_path = os.path.join(self.theme_library_path, new_name);
            if not os.path.exists(new_path): return new_path
            counter += 1

    def build_theme(self):
        """Validates, processes, and saves the complete theme folder."""
        is_valid, error_message = self.validate_assets_on_build();
        if not is_valid: QMessageBox.warning(self, "Validation Error", error_message); return
        title = self.asset_widgets['m_title'].text(); author = self.asset_widgets['m_author'].text(); version = self.asset_widgets['m_version'].text()
        if self.current_theme_path: theme_path = self.current_theme_path
        else: theme_path = self.get_unique_theme_path(title)
        os.makedirs(theme_path, exist_ok=True)
        theme_data = {'title': title, 'author': author, 'version': version, 'assets': {}, 'colors': {}, 'page_waves': {}, 'page_colors': {}}
        # Loop start only applies when encoding to AT9; .at9 files are copied as-is
        if self.bgm_needs_encoding(self.asset_widgets['bgm.at9'].text()):
            try: theme_data['bgm_loop_start'] = int(self.asset_widgets['m_bgmStartSample'].text() or '0')
            except ValueError: theme_data['bgm_loop_start'] = 0
        else: theme_data['bgm_loop_start'] = None
        theme_data['use_global_font_color'] = self.asset_widgets['use_global_font_color'].isChecked()
        def process_and_save(src_path, dest_path, resize_dim):
            try:
                with Image.open(src_path) as img:
                    if img.size != resize_dim:
                        img = img.resize(resize_dim, Image.Resampling.LANCZOS)
                    if img.mode not in ('P', 'L', 'LA'):
                        img = img.convert("P", palette=Image.ADAPTIVE, colors=256)
                    img.save(dest_path, 'PNG'); return True
            except Exception as e: QMessageBox.critical(self, "File Error", f"Could not process image {src_path}:\n{e}"); return False
        required_previews = ['preview_livearea.png', 'preview_lockscreen.png', 'preview_thumbnail.png']; preview_dims = {'preview_livearea.png':(480,272), 'preview_lockscreen.png':(480,272), 'preview_thumbnail.png':(226,128)}
        for prev_id in required_previews:
            src_path = self.asset_widgets[prev_id].text(); dest_path = os.path.join(theme_path, prev_id);
            if not process_and_save(src_path, dest_path, preview_dims[prev_id]):
                return;
            theme_data['assets'][prev_id] = prev_id
        lock_src_path = self.asset_widgets['lockpaper.png'].text(); home1_src_path = self.asset_widgets['br1.png'].text()
        if not process_and_save(lock_src_path, os.path.join(theme_path, 'lockpaper.png'), (960, 512)): return; theme_data['assets']['lockpaper.png'] = 'lockpaper.png'
        if not process_and_save(home1_src_path, os.path.join(theme_path, 'br1.png'), (960, 544)): return; theme_data['assets']['br1.png'] = 'br1.png'
        if not process_and_save(home1_src_path, os.path.join(theme_path, 'tbr1.png'), (360, 192)): return; theme_data['assets']['tbr1.png'] = 'tbr1.png'; theme_data['page_waves']['br1_wave'] = '12'; theme_data['page_colors']['br1_color'] = self.asset_widgets['m_fontColor'].get_color() # Always add page 1 color/wave
        bgm_src_path = self.asset_widgets['bgm.at9'].text()
        if bgm_src_path:
            bgm_dest_path = os.path.join(theme_path, 'bgm.at9')
            if convert_to_at9(bgm_src_path, bgm_dest_path, theme_data.get('bgm_loop_start')): theme_data['assets']['bgm.at9'] = 'bgm.at9'
            else: QMessageBox.warning(self, "BGM Error", f"Failed to convert/copy BGM file: {bgm_src_path}")
        for i in range(2, 11):
            asset_id = f'br{i}.png'; src_path = self.asset_widgets[asset_id].text();
            if src_path:
                dest_name = f'br{i}.png'; thumb_name = f'tbr{i}.png'
                if not process_and_save(src_path, os.path.join(theme_path, dest_name), (960, 544)): return
                if not process_and_save(src_path, os.path.join(theme_path, thumb_name), (360, 192)): return
                theme_data['assets'][asset_id] = dest_name; theme_data['assets'][f'tbr{i}.png'] = thumb_name
                theme_data['page_waves'][f'br{i}_wave'] = self.asset_widgets[f'br{i}_wave'].currentText()
                if not theme_data['use_global_font_color']: theme_data['page_colors'][f'br{i}_color'] = self.asset_widgets[f'br{i}_color'].get_color()
        asset_dims = {'icon_web.png': (128, 128), 'icon_calendar.png': (128, 128), 'icon_photos.png': (128, 128), 'icon_email.png': (128, 128), 'icon_friend.png': (128, 128), 'icon_cma.png': (128, 128), 'icon_message.png': (128, 128), 'icon_music.png': (128, 128), 'icon_near.png': (128, 128), 'icon_parental.png': (128, 128), 'icon_party.png': (128, 128), 'icon_power.png': (128, 128), 'icon_ps3link.png': (128, 128), 'icon_ps4link.png': (128, 128), 'icon_settings.png': (128, 128), 'icon_trophy.png': (128, 128), 'icon_video.png': (128, 128), 'screen_other.png': (22, 22), 'screen_current.png': (22, 22), 'noNotice.png': (120, 110), 'newNotice.png': (120, 110)}
        for asset_id, dims in asset_dims.items():
            src_path = self.asset_widgets[asset_id].text();
            if src_path:
                if not process_and_save(src_path, os.path.join(theme_path, asset_id), dims): return
                theme_data['assets'][asset_id] = asset_id
        for asset_id, widget in self.asset_widgets.items():
            if isinstance(widget, ColorPickerButton) and asset_id.startswith('m_'): theme_data['colors'][asset_id] = widget.get_color()
        if self.write_theme_xml(theme_path, theme_data): play_done_sound(self); QMessageBox.information(self, "Success", f"Theme '{title}' saved successfully!"); self.show_manager_ui()
        else: QMessageBox.warning(self, "Error", "Failed to save theme.xml.")

    def import_zip_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Theme Zip", "", "Zip Files (*.zip)");
        if path:
            self.import_zip_from_path(path)
    def import_zip_from_path(self, path, fromDownload=False):
        theme_name = os.path.basename(path).replace('.zip', ''); target_path = self.get_unique_theme_path(theme_name)
        try:
            with zipfile.ZipFile(path, 'r') as zip_ref: zip_ref.extractall(target_path)
            if not os.path.exists(os.path.join(target_path, 'theme.xml')):
                shutil.rmtree(target_path)
                QMessageBox.warning(self, "Import Error", "Theme is missing theme.xml.")
                return False
            else:
                theme_data = self.parse_theme_xml(os.path.join(target_path, 'theme.xml'))
                if not self.checkRequirements(target_path, theme_data):
                    shutil.rmtree(target_path)
                    return False
            if fromDownload:
                QMessageBox.information(self, "Success", f"{theme_data.get('title')} was downloaded and imported")
            else:
                QMessageBox.information(self, "Success", f"{theme_data.get('title')} was imported")
            self.load_themes_from_library()
            return True
        except Exception as e:
            shutil.rmtree(target_path, ignore_errors=True)
            QMessageBox.critical(self, "Import Error", f"Failed to import zip: {e}")
            return False
    def import_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Import Theme Folder");
        if not path: return
        theme_name = os.path.basename(path); target_path = self.get_unique_theme_path(theme_name)
        try:
            if not os.path.exists(os.path.join(path, 'theme.xml')):
                QMessageBox.warning(self, "Import Error", "Theme is missing theme.xml.")
                return
            theme_data = self.parse_theme_xml(os.path.join(path, 'theme.xml'))
            if not self.checkRequirements(path, theme_data):
                return
            shutil.copytree(path, target_path)
            QMessageBox.information(self, "Success", f"{theme_data.get('title')} was imported")
            self.load_themes_from_library()
        except Exception as e: QMessageBox.critical(self, "Import Error", f"Failed to copy folder: {e}")

    # --- Bulk import: a folder or zip containing many themes (theme folders and/or theme zips) ---
    def bulk_import_folder_dialog(self):
        path = QFileDialog.getExistingDirectory(self, "Select Folder Containing Themes")
        if path: self.bulk_import(path)
    def bulk_import_zip_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Zip Containing Themes", "", "Zip Files (*.zip *.ZIP)")
        if path: self.bulk_import(path)

    def _collect_theme_dirs(self, root, temp_root, errors, label_root='', depth=0):
        """Recursively finds theme folders (containing theme.xml) under root, extracting any zips found.
        Returns a list of (theme_dir, display_label)."""
        theme_dirs = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip macOS archive metadata and hidden folders
            dirnames[:] = [d for d in dirnames if d != '__MACOSX' and not d.startswith('.')]
            rel = os.path.relpath(dirpath, root)
            label = os.path.join(label_root, rel) if rel != '.' else (label_root or os.path.basename(root))
            if 'theme.xml' in filenames:
                theme_dirs.append((dirpath, label)); dirnames[:] = []  # don't descend into a theme
                continue
            if depth >= 2: continue  # limit zip-in-zip nesting
            for name in filenames:
                if not name.lower().endswith('.zip') or name.startswith('._'): continue
                zip_path = os.path.join(dirpath, name)
                zip_label = os.path.join(label, name) if rel != '.' or label_root else name
                extract_path = tempfile.mkdtemp(dir=temp_root)
                try:
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref: zip_ref.extractall(extract_path)
                except Exception as e:
                    errors.append(f"{zip_label}: could not open zip ({e})"); continue
                found = self._collect_theme_dirs(extract_path, temp_root, errors, zip_label, depth + 1)
                if not found: errors.append(f"{zip_label}: no theme.xml found")
                theme_dirs.extend(found)
        return theme_dirs

    def bulk_import(self, source_path):
        temp_root = tempfile.mkdtemp(prefix='vta_bulk_')
        errors = []; imported = []
        try:
            if os.path.isdir(source_path):
                theme_dirs = self._collect_theme_dirs(source_path, temp_root, errors)
            else:
                # Treat a single zip the same as a folder holding that zip
                zip_dir = os.path.join(temp_root, 'source'); os.makedirs(zip_dir)
                shutil.copy2(source_path, zip_dir)
                theme_dirs = self._collect_theme_dirs(zip_dir, temp_root, errors)
            if not theme_dirs and not errors:
                QMessageBox.warning(self, "Bulk Import", "No themes (theme.xml) were found."); return
            progress = QProgressDialog("Importing themes...", "Cancel", 0, len(theme_dirs), self)
            progress.setWindowModality(Qt.WindowModality.WindowModal); progress.setMinimumDuration(500)
            for i, (theme_dir, label) in enumerate(theme_dirs):
                if progress.wasCanceled(): errors.append("Import cancelled by user."); break
                progress.setValue(i); QApplication.processEvents()
                try:
                    theme_data = self.parse_theme_xml(os.path.join(theme_dir, 'theme.xml'))
                    problem = self.find_theme_problem(theme_dir, theme_data)
                    if problem: errors.append(f"{label}: {problem[1]}"); continue
                    shutil.copytree(theme_dir, self.get_unique_theme_path(label))
                    imported.append(theme_data.get('title') or label)
                except Exception as e:
                    errors.append(f"{label}: {e}")
            progress.setValue(len(theme_dirs))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
        self.load_themes_from_library()
        msg = QMessageBox(self); msg.setWindowTitle("Bulk Import")
        msg.setIcon(QMessageBox.Icon.Information if not errors else QMessageBox.Icon.Warning)
        msg.setText(f"Imported {len(imported)} theme(s)." + (f"\n{len(errors)} item(s) were skipped. See details." if errors else ""))
        details = []
        if imported: details.append("Imported:\n " + '\n '.join(imported))
        if errors: details.append("Skipped:\n " + '\n '.join(errors))
        if details: msg.setDetailedText('\n\n'.join(details))
        msg.exec()

    def edit_theme(self):
        selected_item = self.theme_list.currentItem();
        if not selected_item: return
        theme_path = selected_item.data(Qt.ItemDataRole.UserRole); xml_path = os.path.join(theme_path, 'theme.xml');
        if not os.path.exists(xml_path): QMessageBox.warning(self, "Error", "Cannot edit theme: theme.xml not found."); return
        theme_data = self.parse_theme_xml(xml_path); theme_data['path'] = theme_path; self.show_creator_ui(theme_data=theme_data)
    def delete_theme(self):
        selected_item = self.theme_list.currentItem();
        if not selected_item: return
        # GET NAME OF THEME
        theme_path = selected_item.data(Qt.ItemDataRole.UserRole)
        xml_path = os.path.join(theme_path, 'theme.xml')
        theme_data = self.parse_theme_xml(xml_path)
        theme_name = theme_data.get('title')
        reply = QMessageBox.warning(self, "Confirm Delete", f"Are you sure you want to permanently delete '{theme_name}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No);
        if reply == QMessageBox.StandardButton.Yes:
            try: shutil.rmtree(theme_path); QMessageBox.information(self, "Deleted", f"Theme '{theme_name}' has been deleted."); self.load_themes_from_library(); self.btn_edit_theme.setEnabled(False); self.btn_play_bgm.setEnabled(False); self.btn_extract_bgm.setEnabled(False); self.btn_send_to_vita.setEnabled(False); self.btn_delete_theme.setEnabled(False)
            except Exception as e: QMessageBox.critical(self, "Error", f"Could not delete theme: {e}")

    # -------------------------------------------------------------------------
    # Button Slots & Helper Functions
    # -------------------------------------------------------------------------
    def on_selection_changed(self):
        """Enables/disables actions based on the *current* selection."""
        item = self.theme_list.currentItem() # Get the currently selected item

        if not item:
            # If no item is selected, disable all actions and buttons
            self.btn_edit_theme.setEnabled(False); self.edit_theme_action.setEnabled(False)
            self.btn_play_bgm.setEnabled(False); self.play_bgm_action.setEnabled(False)
            self.btn_extract_bgm.setEnabled(False); self.extract_bgm_action.setEnabled(False)
            self.btn_send_to_vita.setEnabled(False); self.send_to_vita_action.setEnabled(False)
            self.btn_submit_theme.setEnabled(False); self.submit_theme_action.setEnabled(False) # <-- Disable new
            self.btn_delete_theme.setEnabled(False); self.delete_theme_action.setEnabled(False)
            return

        # If we are here, an item *is* selected. Check its properties.
        theme_path = item.data(Qt.ItemDataRole.UserRole)
        has_xml = theme_path and os.path.exists(os.path.join(theme_path, 'theme.xml'))
        has_bgm = has_xml and os.path.exists(os.path.join(theme_path, 'bgm.at9'))

        self.btn_edit_theme.setEnabled(has_xml)
        self.edit_theme_action.setEnabled(has_xml)

        self.btn_play_bgm.setEnabled(has_bgm)
        self.play_bgm_action.setEnabled(has_bgm)

        self.btn_extract_bgm.setEnabled(has_bgm)
        self.extract_bgm_action.setEnabled(has_bgm)

        self.btn_send_to_vita.setEnabled(has_xml)
        self.send_to_vita_action.setEnabled(has_xml)

        can_submit = bool(has_xml) and ONLINE_FEATURES_AVAILABLE
        self.btn_submit_theme.setEnabled(can_submit) # <-- Enable new
        self.submit_theme_action.setEnabled(can_submit) # <-- Enable new

        self.btn_delete_theme.setEnabled(True)
        self.delete_theme_action.setEnabled(True)
    def open_settings(self): dialog = SettingsDialog(self, ai_icons=ICON_AI_AVAILABLE); dialog.exec(); self.show_manager_ui()
    def open_about(self): dialog = AboutDialog(self); dialog.exec()
    def open_downloader(self):
        if not ONLINE_FEATURES_AVAILABLE: QMessageBox.information(self, "Unavailable", ONLINE_FEATURES_UNAVAILABLE_MSG); return
        dialog = ThemeDownloaderDialog(self, self); dialog.exec()
    def import_from_vita(self):
        try:
            vita_ip = self.settings.get('vita_ip'); vita_port = int(self.settings.get('vita_port', '1337'));
            if not vita_ip:
                QMessageBox.warning(self, "Error", "PS Vita IP address is not set. Please set it in Settings."); return
            dialog = VitaImporterDialog(vita_ip, vita_port, self, self); dialog.exec()
        except Exception as e: QMessageBox.critical(self, "Error", f"Could not open importer: {e}")
    def apply_settings_to_creator(self): self.asset_widgets['m_author'].setText(self.settings.get('default_author', 'Your Name'))
    def play_bgm(self):
        selected_item = self.theme_list.currentItem();
        if not selected_item: return
        theme_path = selected_item.data(Qt.ItemDataRole.UserRole)
        xml = self.parse_theme_xml(os.path.join(theme_path, 'theme.xml'))
        if xml['assets'].get('bgm.at9') == None:
            QMessageBox.critical(self, "Error", "AT9 not found in the theme xml.")
            return
        bgm_at9_path = os.path.join(theme_path, xml['assets'].get('bgm.at9'))
        # Try to get title from list item text (more reliable than UserRole+1 here)
        theme_title = selected_item.text().split('\nby ')[0]
        if not os.path.exists(bgm_at9_path):
            QMessageBox.warning(self, "Error", "bgm.at9 not found in the theme folder.")
            return
        temp_wav_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); temp_wav_path = temp_wav_file.name; temp_wav_file.close()
        progress = QProgressDialog("Converting audio...", None, 0, 0, self)
        progress.setWindowTitle("Processing")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        def _bgm_status(msg):
            progress.setLabelText(msg)
            QApplication.processEvents()
        ok = convert_at9_to_wav(bgm_at9_path, temp_wav_path, status_callback=_bgm_status)
        progress.close()
        if ok:
            player_dialog = AudioPlayerDialog(temp_wav_path, theme_title, self)
            if getattr(player_dialog, 'init_ok', False):
                player_dialog.exec()  # temp wav cleaned in AudioPlayerDialog.closeEvent
            else:
                # VLC unavailable: the dialog showed the error and never opened;
                # clean up the converted temp wav here.
                removeTemp(temp_wav_path)
        else:
            removeTemp(temp_wav_path)
            QMessageBox.critical(self, "Playback Error", "Failed to convert bgm.at9 to WAV for playback.");
    def extract_bgm(self):
        selected_item = self.theme_list.currentItem();
        if not selected_item: return
        theme_path = selected_item.data(Qt.ItemDataRole.UserRole)
        xml = self.parse_theme_xml(os.path.join(theme_path, 'theme.xml'))
        if xml['assets'].get('bgm.at9') == None:
            QMessageBox.critical(self, "Error", "AT9 not found in the theme xml.")
            return
        bgm_at9_path = os.path.join(theme_path, xml['assets'].get('bgm.at9'))
        theme_title = selected_item.text().split('\nby ')[0]
        safe_title = "".join(c for c in theme_title if c.isalnum() or c in (' ', '_')).rstrip()
        if not os.path.exists(bgm_at9_path):
            QMessageBox.critical(self, "Error", f"{xml['assets'].get('bgm.at9')} not found in the theme folder.")
            return
        save_path, _ = QFileDialog.getSaveFileName(self, "Save BGM As WAV", f"{safe_title}_bgm.wav", "WAV Files (*.wav)");
        if save_path:
            progress = QProgressDialog("Converting audio...", None, 0, 0, self)
            progress.setWindowTitle("Processing")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.show()
            QApplication.processEvents()
            def _extract_status(msg):
                progress.setLabelText(msg)
                QApplication.processEvents()
            ok = convert_at9_to_wav(bgm_at9_path, save_path, status_callback=_extract_status)
            progress.close()
            if ok: QMessageBox.information(self, "Success", f"BGM extracted successfully to:\n{save_path}")
            else: QMessageBox.critical(self, "Extraction Error", "Failed to convert bgm.at9 to WAV.")

    # --- NEW REPOSITORY SUBMISSION LOGIC ---

    def cleanup_temp_zip(self, *args):
        """Cleans up the temporary zip file after the webview window is closed."""
        if hasattr(self, 'temp_zip_for_upload') and self.temp_zip_for_upload:
            if os.path.exists(self.temp_zip_for_upload):
                try:
                    os.remove(self.temp_zip_for_upload)
                    print(f"Cleaned up temp zip: {self.temp_zip_for_upload}")
                except Exception as e:
                    print(f"Error cleaning up zip: {e}")
            self.temp_zip_for_upload = None

    def submit_theme_to_repo(self):
        """Packages the theme and opens the correct repository submission URL."""
        if not ONLINE_FEATURES_AVAILABLE: QMessageBox.information(self, "Unavailable", ONLINE_FEATURES_UNAVAILABLE_MSG); return
        selected_item = self.theme_list.currentItem()
        navRemoval = r'''
        document.querySelectorAll(".ui-bar-a, .ui-page-theme-a .ui-bar-inherit, html .ui-bar-a .ui-bar-inherit, html .ui-body-a .ui-bar-inherit, html body .ui-group-theme-a .ui-bar-inherit").forEach(function(i) {if($(i.parentNode).attr('data-url') !== "/submit.php") {i.remove()}});
        '''
        paddingChange = r"""
        const submitPage = document.getElementById('submit');
        submitPage.style.paddingTop = '0px'
        """
        navTheme = r"""
        (function tryClick(attempts) {
            var btn = document.querySelector('.ui-input-btn input');
            if (btn) { btn.click(); return; }
            if (attempts > 0) setTimeout(function() { tryClick(attempts - 1); }, 300);
        })(15);
        """

        base64ToFile = r"""
        function base64ToFile(base64, filename, mimeType) {
            // 1. Decode the (Base64 string to a binary string
            const byteCharacters = atob(base64);

            // 2. Create an ArrayBuffer to hold the binary data
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            const byteArray = new Uint8Array(byteNumbers);

            // 3. Create a Blob (a file-like object) from the binary data
            const blob = new Blob([byteArray], { type: mimeType });

            // 4. Create the final File object from the Blob
            // The File object is derived from Blob, but also includes name and lastModifiedDate.
            return new File([blob], filename, { type: mimeType });
        }
        """
        guessMimeType = r"""
        function guessMimeType(filename) {
            const ext = filename.split('.').pop().toLowerCase();
            switch (ext) {
                case 'zip': return 'application/zip'
                default: return 'error';
            }
        }
        """

        cancelButton = r"""
        document.querySelectorAll(".ui-page-theme-a .ui-btn, html .ui-bar-a .ui-btn, html .ui-body-a .ui-btn, html body .ui-group-theme-a .ui-btn, html head + body .ui-btn.ui-btn-a, .ui-page-theme-a .ui-btn:visited, html .ui-bar-a .ui-btn:visited, html .ui-body-a .ui-btn:visited, html body .ui-group-theme-a .ui-btn:visited, html head + body .ui-btn.ui-btn-a:visited").forEach(function(i) {if($(i).attr('href') === "./index.php") {
            i.onclick = function (e) { e.preventDefault(); };

            i.addEventListener('click', async() => {
                await pywebview.api.cancel()
            })
        }});
        """
        fileText = r"""
        $('label[for="file"]').get()[0].innerText = "Your Custom Theme's package:"
        """

        openFileButton = r"""
        const openFileButton = document.getElementById('upl');
        openFileButton.onclick = function (e) { e.preventDefault(); };
        openFileButton.ondrop = function (e) { e.preventDefault(); };
        """

        getcontent = r"""
        async function fetchData() {
            try {
                const result = await pywebview.api.get_content();
                if (result.status === 'success') {
                    const filename = result.filename;
                    const base64Content = result.content;
                    const mimeType = guessMimeType(filename);
                    if (mimeType !== 'error') {
                        const fileObject = base64ToFile(base64Content, filename, mimeType);
                        let s = document.getElementById('upl');
                        let list = new DataTransfer();
                        list.items.add(fileObject);
                        s.files = list.files;
                    }
                }
            } catch (_) {}
            await pywebview.api.ready();
        }
        """

        if not selected_item:
            return

        _main = self  # capture outer VitaThemeArchitect instance for use inside Api

        def removeNav(win):
            win.run_js(navRemoval)
            win.run_js(paddingChange)
            win.show()
            win.events.loaded -= loaded

        def navToUpdate(win):
            win.run_js(navTheme)
            win.events.loaded -= loaded

        combined = (cancelButton + fileText + base64ToFile + guessMimeType
                    + openFileButton + getcontent + "fetchData();")

        def onSubmitLoaded():
            # Fires when any page loads. Only act when we reach submit.php —
            # loaded guarantees the DOM is fully ready so no polling needed.
            current = window.get_current_url() or ""
            if not current.startswith("https://psvt.ovh/submit.php"):
                return
            if window.runOnce > 0:
                return
            window.runOnce += 1
            window.events.loaded -= onSubmitLoaded
            if not window.isUpdate:
                window.hide()
            window.run_js(combined)

        def onChange(win):
            import threading
            current = win.get_current_url() or ""
            # Update path only: loaded is unreliable for the initial hidden page
            # on macOS, so use response_received to detect theme.php and trigger
            # the JS click that navigates to submit.php.
            if win.isUpdate and "psvt.ovh/theme.php" in current and not win.themePageHandled:
                win.themePageHandled = True
                win.events.response_received -= received
                threading.Timer(1.0, lambda: win.run_js(navTheme)).start()

        class Api:
            def ready(self):
                window.show()
            def cancel(self):
                window.destroy()
            def get_content(self):
                if not os.path.isfile(_main.temp_zip_for_upload):
                    return {'status': 'cancelled'}
                file_name = os.path.basename(_main.temp_zip_for_upload)
                with open(_main.temp_zip_for_upload, 'rb') as f:
                    file_content = f.read()
                base64_content = base64.b64encode(file_content).decode('utf-8')
                return {
                    'filename': file_name,
                    'content': base64_content,
                    'status': 'success'
                }

        local_theme_path = selected_item.data(Qt.ItemDataRole.UserRole)
        xml_path = os.path.join(local_theme_path, 'theme.xml')
        theme_data = self.parse_theme_xml(xml_path)
        theme_title = theme_data.get('title')

        if not theme_title:
            QMessageBox.warning(self, "Error", "Cannot submit theme: Title is missing from theme.xml.")
            return

        # 1. Create the temporary zip file
        safe_title = "".join(c for c in theme_title if c.isalnum() or c in (' ', '_')).rstrip()
        self.temp_zip_for_upload = os.path.join(tempfile.gettempdir(), f"{safe_title}_upload.zip")

        try:
            with zipfile.ZipFile(self.temp_zip_for_upload, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, _, files in os.walk(local_theme_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        # arcname makes it relative to the theme folder
                        arcname = os.path.relpath(file_path, local_theme_path)
                        zipf.write(file_path, arcname)
        except Exception as e:
            QMessageBox.critical(self, "Zip Error", f"Failed to create theme zip: {e}")
            self.cleanup_temp_zip()
            return

        # 2. Check API for theme name
        try:
            # Only themes whose title contains this one (the list is searched, not all fetched).
            all_themes = list(theme_pages(theme_title, 'title'))
        except AttributeError:
            QMessageBox.warning(self, "Error", "Conditions not met.")
            self.cleanup_temp_zip()
            return
        except Exception as e:
            QMessageBox.critical(self, "API Error", f"Could not fetch theme list from repository: {e}")
            self.cleanup_temp_zip()
            return

        matches = [theme for theme in all_themes if theme.get('title', '').lower() == theme_title.lower()]

        target_url = ""
        # 3. Determine URL based on matches
        if not matches:
            # No match, it's a new theme
            target_url = "https://psvt.ovh/index.php#submit"
            loaded = lambda: removeNav(window)
        else:
            # Match found: show the existing theme(s) with previews so the user
            # can see what they'd update, and choose (picking which one if several).
            dialog = ThemeExistsDialog(self, theme_title, matches)
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.action == 'cancel':
                self.cleanup_temp_zip()
                return
            if dialog.action == 'new':
                target_url = "https://psvt.ovh/index.php#submit"
                loaded = lambda: removeNav(window)
            else:   # update the chosen existing theme
                selected_id = dialog.selected_theme.get('id')
                target_url = f"https://psvt.ovh/theme.php?id={selected_id}"
                loaded = lambda: navToUpdate(window)

        # 4. Open pywebview window and provide zip path
        if target_url:
            try:
                window = webview.create_window('Submit Theme to PSVT', target_url, confirm_close=True, hidden=True, resizable=False, js_api=Api())
                window.isUpdate = target_url != "https://psvt.ovh/index.php#submit"
                window.themePageHandled = False
                window.runOnce = 0
                window.events.closed += self.cleanup_temp_zip
                window.events.loaded += loaded
                window.events.loaded += onSubmitLoaded
                received = lambda: onChange(window)
                window.events.response_received += received
                webview.start()
            except Exception as e:
                self.cleanup_temp_zip()
                QMessageBox.critical(self, "Webview Error", f"Could not open submission window: {e}")
        else:
            # This should not happen if logic is correct, but as a fallback
            self.cleanup_temp_zip()
            QMessageBox.warning(self, "Error", "Could not determine submission URL. Zip file will be cleaned up.")

    # --- END NEW LOGIC ---

    def send_to_vita(self):
        """Transfers the selected theme folder to the PS Vita via FTP with version check."""
        selected_item = self.theme_list.currentItem();
        if not selected_item: return
        local_theme_path = selected_item.data(Qt.ItemDataRole.UserRole)
        try:
            vita_ip = self.settings.get('vita_ip'); vita_port = int(self.settings.get('vita_port', '1337'))
            if not vita_ip: QMessageBox.warning(self, "Error", "PS Vita IP address is not set. Please set it in Settings."); return
            local_xml_path = os.path.join(local_theme_path, 'theme.xml'); local_data = self.parse_theme_xml(local_xml_path); local_version = local_data.get('version', '00.00'); local_theme_title = local_data.get('title', os.path.basename(local_theme_path))
            remote_base_path = 'ux0:customtheme'; remote_theme_path_base = f"theme" # Use actual title
            ftp = ftplib.FTP()
            ftp.connect(vita_ip, vita_port, timeout=20)
            ftp.login()
            try:
                ftp.cwd(remote_base_path)
            except ftplib.error_perm:
                ftp.mkd(remote_base_path)
                ftp.cwd(remote_base_path)
            remote_theme_path = remote_theme_path_base; overwrite_existing = False; counter = 0
            ignore = False
            while True: # Find unique remote path or check version
                try:
                    ftp.cwd(remote_theme_path);
                    remote_xml_data = io.BytesIO()
                    ftp.retrbinary('RETR theme.xml', remote_xml_data.write)
                    remote_xml_data.seek(0)
                    if ignore:
                        continue
                    with open("temp_theme.xml", "wb") as f:
                        f.write(remote_xml_data.getvalue())
                    remote_data = self.parse_theme_xml("temp_theme.xml"); os.remove("temp_theme.xml")
                    remote_version = remote_data.get('version', '00.00')
                    remote_title = remote_data.get('title')
                    if float(local_version) > float(remote_version) and remote_title == local_theme_title:
                        reply = QMessageBox.question(self, "Confirm Overwrite", f"Local theme '{local_theme_title}' (v{local_version}) is newer than the one on Vita (v{remote_version}) at '{remote_theme_path}'.\n\nOverwrite it?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                        if reply == QMessageBox.StandardButton.Yes: overwrite_existing = True; break
                        else:
                            reply = QMessageBox.question(self, "Send to Vita", f"Send to a seperate folder then {remote_theme_path}?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                            if reply == QMessageBox.StandardButton.Yes:
                                ignore = True
                            else:
                                ftp.quit(); return
                    elif float(local_version) == float(remote_version) and remote_title == local_theme_title:
                        reply = QMessageBox.question(self, "Confirm Overwrite", f"Local theme '{local_theme_title}' (v{local_version}) already exists at '{remote_theme_path}'.\n\nOverwrite it?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                        if reply == QMessageBox.StandardButton.Yes: overwrite_existing = True; break
                        else:
                            reply = QMessageBox.question(self, "Send to Vita", f"Send to a seperate folder then {remote_theme_path}?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                            if reply == QMessageBox.StandardButton.Yes:
                                ignore = True
                            else:
                                ftp.quit(); return
                    elif float(local_version) < float(remote_version) and remote_title == local_theme_title:
                        QMessageBox.warning(self, "Upload Error", f"{local_theme_title} already has a newer version '{remote_version}.'")
                        ftp.quit()
                        return
                    else:
                        counter += 1;
                        remote_theme_path = f"{remote_theme_path_base}{counter}"
                        ftp.cwd('..')
                        continue # Try next sequential name
                except ftplib.error_perm:
                    ftp.mkd(remote_theme_path)
                    overwrite_existing = False; break # Folder doesn't exist or, create
                except Exception as e:
                     reply = QMessageBox.warning(self, "Remote XML Error", f"Could not read theme.xml for '{remote_theme_path}' on Vita: {e}.\n\nTry to overwrite this folder?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                     if reply == QMessageBox.StandardButton.Yes: overwrite_existing = True; break
                     else: counter += 1; remote_theme_path = f"{remote_theme_path_base}{counter}"; ftp.cwd('..'); continue
            ftp.cwd(remote_theme_path) # Go into target dir
            if overwrite_existing:
                print(f"Overwriting theme at {remote_theme_path}...")
                try:
                    lines = []
                    items = []
                    self.ftp.retrlines(f"LIST", callback=lambda line: lines.append(line))
                    for line in lines:
                        parts = line.strip().split(maxsplit=8)
                        if len(parts) == 9:
                            name = parts[-1].strip()
                            if name not in ('.', '..'):
                                items.append(name)
                    for item in items:
                        try: ftp.delete(item)
                        except ftplib.error_perm:
                            print(f"Could not delete {item}, might be a directory.")
                except Exception as del_e:
                    print(f"Error while cleaning remote directory: {del_e}")
            else:
                print(f"Creating new theme at {remote_theme_path}...")
            files_to_upload = [f for f in os.listdir(local_theme_path) if os.path.isfile(os.path.join(local_theme_path, f))];
            progress = QProgressDialog(f"Transferring '{local_theme_title}' to {remote_theme_path}", "Cancel", 0, len(files_to_upload), self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            for i, filename in enumerate(files_to_upload):
                progress.setLabelText(f"Uploading {filename}..."); local_file = os.path.join(local_theme_path, filename)
                with open(local_file, 'rb') as f:
                    ftp.storbinary(f'STOR {filename}', f)
                progress.setValue(i + 1);
                if progress.wasCanceled():
                    ftp.quit()
                    break
            if progress.wasCanceled():
                ftp.quit(); progress.close()
                QMessageBox.warning(self, "Cancelled", "Theme transfer was cancelled.")
            else:
                ftp.quit(); progress.close()
                QMessageBox.information(self, "Transfer Complete", f"Theme '{local_theme_title}' sent to Vita successfully at:\n{remote_theme_path}")
        except Exception as e:
            QMessageBox.critical(self, "FTP Transfer Error", f"Could not send theme:\n{e}");
        if 'ftp' in locals() and ftp.sock:
            ftp.quit()

    # -------------------------------------------------------------------------
    # Update Check Logic
    # -------------------------------------------------------------------------
    def _is_official_build(self):
        """Official = a frozen build that ships the secure modules (crypted set).
        Unofficial / source builds have crypted is None and their version number
        can be hand-edited, so the in-app updater is disabled for them — it must
        never replace an untested or modified build with a release, or run
        migrations keyed off a faked version."""
        return app_updater.running_frozen() and (crypted is not None)

    def check_for_updates(self, manual=False):
        """Check GitHub for a newer release and offer to install it.

        `manual=True` (from Settings) reports "up to date"/errors to the user;
        the silent startup check stays quiet unless an update is found."""
        if not self._is_official_build():
            print("In-app updater disabled: not an official build.")
            if manual:
                QMessageBox.information(
                    self, "Updates Unavailable",
                    "In-app updates are only available in official builds.\n\n"
                    "This looks like an unofficial or source build, so automatic "
                    "updating is turned off — it won't replace this build with a "
                    "release.")
            return
        if not manual and not self.settings.get('auto_update_check', True):
            print("Auto-update check disabled in settings."); return
        print("Checking for updates...")
        try:
            rel = app_updater.fetch_latest()
        except Exception as e:
            print(f"Update check failed: {e}")
            if manual:
                QMessageBox.warning(self, "Update Check Failed",
                                    f"Couldn't check for updates:\n{e}")
            return
        tag = rel.get('tag') or ''
        print(f"Latest version found: {tag}, Current version: v{CURRENT_VERSION}")
        if not app_updater.is_newer(tag, CURRENT_VERSION):
            print("Application is up to date.")
            if manual:
                QMessageBox.information(self, "No Updates",
                                       f"You're up to date (v{CURRENT_VERSION}).")
            return
        self.offer_update(rel)

    def offer_update(self, rel):
        """Show the new version with a short summary of changes and offer to
        download + install it."""
        tag = rel.get('tag') or ''
        body = rel.get('body', '') or ''
        # Short, clean summary shown inline (a few points). If there's more, a
        # "Full changelog" button opens the complete notes IN-APP (no browser /
        # repo page), so the dialog stays tidy at any changelog length.
        short = app_updater.summarize_notes(body, max_lines=6, max_chars=1500)
        full = app_updater.summarize_notes(body, max_lines=1000, max_chars=20000)
        has_more = bool(full) and full != short

        dlg = QDialog(self)
        dlg.setWindowTitle("Update Available")
        lay = QVBoxLayout(dlg)

        header = QLabel(f"Version {tag} is available — you have v{CURRENT_VERSION}.")
        header.setStyleSheet("font-weight: bold;")
        header.setWordWrap(True)
        lay.addWidget(header)

        if short:
            lay.addWidget(QLabel("What's new:"))
            # `short` already ends with its own "…" when it was truncated — don't
            # add a second one.
            summary = QLabel(short)
            summary.setWordWrap(True)
            summary.setStyleSheet("color: #cfd8e8;")
            lay.addWidget(summary)

        if has_more:
            full_btn = QPushButton("Full changelog")
            full_btn.setFlat(True)
            full_btn.clicked.connect(lambda: self._show_full_changelog(tag, full))
            full_row = QHBoxLayout()
            full_row.addWidget(full_btn)
            full_row.addStretch(1)
            lay.addLayout(full_row)

        lay.addSpacing(4)
        prompt = QLabel("Download and install it now?")
        prompt.setWordWrap(True)
        lay.addWidget(prompt)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        buttons.button(QDialogButtonBox.StandardButton.Yes).setDefault(True)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)

        dlg.setMinimumWidth(440)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.download_and_apply_update(rel)

    def _show_full_changelog(self, tag, notes):
        """Show the complete release notes in a scrollable in-app dialog (no
        browser, so it works for a private repo and never leaks the location)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"What's new in {tag}")
        lay = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setPlainText(notes)
        view.setReadOnly(True)
        view.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        lay.addWidget(view)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dlg.reject)
        close.accepted.connect(dlg.accept)
        lay.addWidget(close)
        dlg.resize(520, 460)
        dlg.exec()

    def _build_edition(self):
        """'ai' when this build has the icon generator, else 'standard'."""
        return 'ai' if ICON_AI_AVAILABLE else 'standard'

    def download_and_apply_update(self, rel):
        """Normal update: the newer build of the SAME edition this one is."""
        asset = app_updater.pick_asset(rel.get('assets', []), edition=self._build_edition())
        self._install_release(rel, asset, clean=False)

    def _install_release(self, rel, asset, clean=False, before_apply=None):
        """Shared installer for updates and edition switches: download, extract,
        then hand off to the detached helper and quit.

        `clean=True` mirrors the install folder (removes files from the previous
        edition); `before_apply` runs just before the handoff (used to free AI
        data on a switch)."""
        tag = rel.get('tag') or ''
        if not app_updater.running_frozen():
            if QMessageBox.question(
                    self, "Update",
                    "You're running from source. Open the releases page to update manually?"
            ) == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(UPDATE_URL))
            return
        if not asset:
            os_name, arch = app_updater.platform_tag()
            if QMessageBox.question(
                    self, "No Matching Download",
                    f"Release {tag} has no matching build for your system ({os_name}-{arch}).\n\n"
                    "Open the releases page to check manually?"
            ) == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl(UPDATE_URL))
            return

        progress = QProgressDialog(f"Downloading {tag}…", "Cancel", 0, 100, self)
        # Lock the whole app while updating — only the Cancel button is usable.
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        def on_progress(done, total):
            if total:
                progress.setMaximum(total)
                progress.setValue(done)
            else:
                progress.setMaximum(0)  # indeterminate when size unknown
            QApplication.processEvents()

        try:
            result = app_updater.prepare_update(
                asset, progress=on_progress, cancel=progress.wasCanceled)
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Update Error",
                                 f"Could not download or extract the build:\n{e}")
            return
        progress.close()
        if not result:
            return  # cancelled
        new_app, stage_root = result

        # No extra "Continue?" prompt — the user already chose to update, and the
        # whole thing runs in the foreground (the app closes and reopens itself).
        # Apply first: this is the only step that may ask for elevation (when the
        # install folder isn't user-writable, e.g. an all-users / Program Files
        # install). The detached helper waits for this process to exit before it
        # copies, so the teardown/`before_apply` below still run first. On Windows a
        # declined prompt returns False here, so we abort cleanly — nothing torn
        # down, no data freed.
        try:
            started = app_updater.apply_and_restart(new_app, stage_root, clean=clean)
        except Exception as e:
            shutil.rmtree(stage_root, ignore_errors=True)
            QMessageBox.critical(self, "Update Error",
                                 f"Could not apply the build:\n{e}")
            return
        if not started:
            shutil.rmtree(stage_root, ignore_errors=True)
            QMessageBox.information(
                self, "Update",
                "This update needs administrator permission, which was declined.\n\n"
                "No changes were made.")
            return
        # Proceeding: free AI data on a switch, run the same teardown a normal close
        # does (stop Wine, clear temp), then quit so the helper can swap the folder.
        if before_apply:
            before_apply()
        self._run_exit_cleanup()
        QApplication.quit()

    @staticmethod
    def _human_size(nbytes):
        size = float(nbytes)
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if size < 1024 or unit == 'TB':
                return f"{size:.0f} {unit}" if unit in ('B', 'KB') else f"{size:.1f} {unit}"
            size /= 1024

    def _run_exit_cleanup(self):
        """Shared teardown for a normal close AND an update/edition-switch
        restart: drop generated-icon temp folders, remove the upload temp zip,
        and stop Wine (non-Windows) so nothing holds files in the install folder
        while it is swapped out. Best-effort — never raises."""
        try:
            self.clear_generated_icons()
        except Exception as e:
            print(f"Icon cleanup skipped: {e}")
        try:
            self.cleanup_temp_zip()
        except Exception as e:
            print(f"Temp zip cleanup skipped: {e}")
        import platform
        if platform.system() != 'Windows':
            try:
                shutdown_wine()
            except Exception as e:
                print(f"Wine shutdown skipped: {e}")

    def closeEvent(self, event):
        """Stop Wine processes and clean up the persistent Wine directory on exit."""
        self._run_exit_cleanup()
        super().closeEvent(event)