import os
import ftplib

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QListWidget, QPushButton,
    QAbstractItemView, QProgressDialog, QMessageBox, QApplication
)
from PyQt6.QtCore import Qt


class VitaImporterDialog(QDialog):
    def __init__(self, vita_ip, vita_port, main_window, parent=None):
        super().__init__(parent)
        self.vita_ip = vita_ip
        self.vita_port = vita_port
        self.main_window = main_window
        self.setWindowTitle("📡 Import from PS Vita")
        self.setGeometry(200, 200, 500, 600)
        layout = QVBoxLayout(self)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(
            "font-size: 12px; padding: 4px 6px; border-radius: 4px; color: #aaaacc;"
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.vita_theme_list = QListWidget()
        self.vita_theme_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.vita_theme_list)
        self.import_button = QPushButton("Download && Import Selected Themes")
        self.import_button.setEnabled(False); self.import_button.clicked.connect(self.start_import)
        layout.addWidget(self.import_button)
        self.ftp = None
        self._set_status(f"Connecting to {self.vita_ip}:{self.vita_port}...")
        self.connect_and_list_themes()
    def _set_status(self, message, kind="info"):
        """Update status_label with color-coded styling. kind: 'info' | 'success' | 'error'"""
        styles = {
            "info":    "color: #aaaacc; background: transparent;",
            "success": "color: #66bb6a; background: transparent;",
            "error":   "color: #ef5350; background: transparent;",
        }
        self.status_label.setStyleSheet(
            "font-size: 12px; padding: 4px 6px; border-radius: 4px; "
            + styles.get(kind, styles["info"])
        )
        self.status_label.setText(message)

    def showEvent(self, event):
        super().showEvent(event)
        from widgets import center_on_parent
        center_on_parent(self)
    def connect_and_list_themes(self):
        try:
            self.ftp = ftplib.FTP()
            self.ftp.connect(self.vita_ip, self.vita_port, timeout=10)
            self.ftp.login()
            self.ftp.cwd('ux0:customtheme')
            theme_folders = []
            lines = []
            self.ftp.retrlines(f"LIST", callback=lambda line: lines.append(line))
            for line in lines:
                parts = line.strip().split(maxsplit=8)
                if len(parts) == 9:
                    name = parts[-1].strip()
                    if name not in ('.', '..'):
                        theme_folders.append(name)
            self.vita_theme_list.addItems(theme_folders)
            self._set_status(f"Connected — {len(theme_folders)} theme(s) found. Select to import.", "success")
            self.import_button.setEnabled(True)
        except Exception as e: self._set_status(f"Connection failed: {e}", "error"); QMessageBox.critical(self, "FTP Error", f"Could not connect or list themes:\n{e}"); self.import_button.setEnabled(False)
    def start_import(self):
        selected_items = self.vita_theme_list.selectedItems();
        if not selected_items: QMessageBox.warning(self, "No Selection", "Please select one or more themes to import."); return
        selected_themes = [item.text() for item in selected_items]
        files_to_download = []
        for theme in selected_themes:
            self.ftp.cwd(theme)
            lines = []
            self.ftp.retrlines(f"LIST", callback=lambda line: lines.append(line))
            for line in lines:
                parts = line.strip().split(maxsplit=8)
                if len(parts) == 9:
                    name = parts[-1].strip()
                    if name not in ('.', '..'):
                        files_to_download.append(name)
            self.ftp.cwd('..')
        progress = QProgressDialog("Importing themes from Vita...", "Cancel", 0, len(files_to_download), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        current = 0
        try:
            for theme_name in selected_themes:
                progress.setLabelText(f"Importing '{theme_name}'...")
                QApplication.processEvents()
                local_path = self.main_window.get_unique_theme_path(theme_name); os.makedirs(local_path, exist_ok=True); remote_path = theme_name
                try:
                    self.ftp.cwd(remote_path)
                    files_to_download = []
                    lines = []
                    self.ftp.retrlines(f"LIST", callback=lambda line: lines.append(line))
                    for line in lines:
                        parts = line.strip().split(maxsplit=8)
                        if len(parts) == 9:
                            name = parts[-1].strip()
                            if name not in ('.', '..'):
                                files_to_download.append(name)
                    if progress.isVisible():
                        progress.show()
                    for filename in files_to_download:
                        local_file = os.path.join(local_path, filename);
                        with open(local_file, 'wb') as f:
                            self.ftp.retrbinary(f'RETR {filename}', f.write)
                        current += 1
                        progress.setValue(current)
                        QApplication.processEvents()
                    self.ftp.cwd('..')
                except Exception as e: print(f"Could not download {theme_name}: {e}")
                QApplication.processEvents()
                if progress.wasCanceled():
                    break
            self.ftp.quit()
            QMessageBox.information(self, "Success", f"Imported {len(selected_themes)} themes."); self.main_window.load_themes_from_library(); self.accept()
        except Exception as e:
            progress.close()
            if self.ftp:
                self.ftp.quit()
            QMessageBox.critical(self, "FTP Error", f"An error occurred during import:\n{e}")
            return
        progress.close()
        self.close()
