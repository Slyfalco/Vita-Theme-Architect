from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt

from constants import CURRENT_VERSION


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ℹ️ About VitaTheme Architect")
        self.setMinimumWidth(400)  # <-- Optional: Add a minimum width
        layout = QVBoxLayout(self)

        title_label = QLabel(f"VitaTheme Architect v{CURRENT_VERSION}")
        title_label.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        layout.addWidget(title_label, 0, Qt.AlignmentFlag.AlignCenter)

        desc_label = QLabel("A tool to create, manage, and download themes for the PS Vita.")
        layout.addWidget(desc_label, 0, Qt.AlignmentFlag.AlignCenter)

        author_label = QLabel("Created by: SlyFalco")
        layout.addWidget(author_label, 0, Qt.AlignmentFlag.AlignCenter)

        links_label = QLabel('<a href="https://github.com/Slyfalco/Vita-Theme-Architect">GitHub Project</a>')
        links_label.setOpenExternalLinks(True)
        layout.addWidget(links_label, 0, Qt.AlignmentFlag.AlignCenter)

        # --- ADD NEW HOTKEY SECTION ---
        layout.addSpacing(15)
        hotkey_title_label = QLabel("<b>Keyboard Shortcuts (Manager)</b>")
        hotkey_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hotkey_title_label)

        hotkey_text = """
        <style>
            table { width: 100%; border-collapse: collapse; margin-left: auto; margin-right: auto; }
            td { padding: 4px; }
            td.key { font-family: 'Courier New', monospace; font-weight: bold; text-align: right; padding-right: 10px; }
            td.desc { text-align: left; }
        </style>
        <table width="300">
            <tr><td class="key">Ctrl+N</td><td class="desc">New Theme</td></tr>
            <tr><td class="key">Ctrl+I</td><td class="desc">Import Theme (.zip)</td></tr>
            <tr><td class="key">Ctrl+O</td><td class="desc">Import Theme (Folder)</td></tr>
            <tr><td class="key">Ctrl+V</td><td class="desc">Import from PS Vita</td></tr>
            <tr><td class="key">Ctrl+D</td><td class="desc">Download Themes</td></tr>
            <tr><td class="key">Ctrl+E</td><td class="desc">Edit Selected Theme</td></tr>
            <tr><td class="key">Ctrl+P</td><td class="desc">Play Selected BGM</td></tr>
            <tr><td class="key">Ctrl+X</td><td class="desc">Extract Selected BGM</td></tr>
            <tr><td class="key">Ctrl+S</td><td class="desc">Send Selected to Vita</td></tr>
            <tr><td class="key">Delete</td><td class="desc">Delete Selected Theme</td></tr>
        </table>
        """
        hotkey_label = QLabel(hotkey_text)
        hotkey_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hotkey_label)
        # --- END NEW SECTION ---

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        button_box.accepted.connect(self.accept)
        layout.addWidget(button_box)

    def showEvent(self, event):
        super().showEvent(event)
        from widgets import center_on_parent
        center_on_parent(self)
