"""Application-wide look and feel.

Uses Qt's built-in Fusion style (compiled into QtWidgets, so no extra style
plugin is needed in Nuitka builds) with a dark palette and a shared stylesheet,
so the app looks the same on Windows, macOS and Linux.

Buttons can opt into an accent colour via the ``role`` dynamic property:
``btn.setProperty("role", "primary" | "success" | "danger" | "link")``.
Side panels use ``role="panel"`` and image placeholders ``role="preview"``.
"""

from PyQt6.QtGui import QPalette, QColor

# Palette shared with the splash screen in main.py
BG = "#1a1a2e"
SURFACE = "#20203a"
SURFACE_ALT = "#26264a"
INPUT = "#151528"
BORDER = "#2e3a5c"
BORDER_HOVER = "#4a5f8f"
TEXT = "#e0e0ff"
TEXT_MUTED = "#8a8aa8"
ACCENT = "#4a90d9"
ACCENT_HOVER = "#5ba0e8"
SUCCESS = "#3fa45b"
SUCCESS_HOVER = "#4cb86a"
DANGER = "#d9534f"
DANGER_HOVER = "#e5645f"
PREVIEW_BG = "#11111f"

STYLESHEET = f"""
QWidget {{
    color: {TEXT};
}}
QMainWindow, QDialog {{
    background-color: {BG};
}}
QToolTip {{
    color: {TEXT};
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    padding: 4px 6px;
    border-radius: 4px;
}}

/* Panels */
QFrame[role="panel"] {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel {{
    background: transparent;
}}

/* Buttons */
QPushButton {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 12px;
}}
QPushButton:hover {{
    border-color: {BORDER_HOVER};
    background-color: #2d2d56;
}}
QPushButton:pressed {{
    background-color: {INPUT};
}}
QPushButton:focus {{
    border-color: {ACCENT};
}}
QPushButton:disabled {{
    color: #5a5a78;
    background-color: {SURFACE};
    border-color: #262640;
}}
QPushButton[role="primary"] {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: white;
    font-weight: bold;
    padding: 10px;
}}
QPushButton[role="primary"]:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton[role="success"] {{
    background-color: {SUCCESS};
    border-color: {SUCCESS};
    color: white;
    font-weight: bold;
    padding: 10px;
}}
QPushButton[role="success"]:hover {{
    background-color: {SUCCESS_HOVER};
}}
QPushButton[role="danger"] {{
    background-color: {DANGER};
    border-color: {DANGER};
    color: white;
}}
QPushButton[role="danger"]:hover {{
    background-color: {DANGER_HOVER};
}}
QPushButton[role="primary"]:disabled,
QPushButton[role="success"]:disabled,
QPushButton[role="danger"]:disabled {{
    color: #5a5a78;
    background-color: {SURFACE};
    border-color: #262640;
}}
QPushButton[role="link"] {{
    background: none;
    border: none;
    padding: 0;
    color: {ACCENT_HOVER};
    text-decoration: underline;
    text-align: left;
}}
QPushButton[role="link"]:hover {{
    color: white;
}}

/* Pager number buttons: small clickable boxes, distinct from the main
   buttons; the current page is filled with the accent colour. */
QPushButton[role="page"] {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 2px 4px;
    color: {TEXT};
    text-decoration: none;
    text-align: center;
}}
QPushButton[role="page"]:hover {{
    border-color: {ACCENT};
    background-color: {SURFACE_ALT};
    color: white;
}}
QPushButton[role="page"][current="true"],
QPushButton[role="page"][current="true"]:hover {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: white;
    font-weight: bold;
}}

/* Inputs */
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{
    background-color: {INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
    border-color: {BORDER_HOVER};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled {{
    color: #5a5a78;
    background-color: {SURFACE};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    outline: none;
}}

/* Lists */
QListWidget {{
    background-color: {INPUT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 6px;
    border-radius: 6px;
}}
QListWidget::item:hover {{
    background-color: {SURFACE_ALT};
}}
QListWidget::item:selected {{
    background-color: #2b4a7a;
    color: white;
}}

/* Tabs */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background-color: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 8px 14px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{
    color: {TEXT};
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}

/* Scroll areas & bars */
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 3px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
    background: {BORDER_HOVER};
}}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
    border: none;
    width: 0;
    height: 0;
}}

/* Checkboxes, sliders, progress */
QCheckBox {{
    spacing: 8px;
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QProgressBar {{
    background-color: {INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 5px;
}}

/* Image preview placeholders */
QLabel[role="preview"] {{
    background-color: {PREVIEW_BG};
    border: 1px dashed {BORDER};
    border-radius: 8px;
    color: {TEXT_MUTED};
}}
"""


def _dark_palette():
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: INPUT,
        QPalette.ColorRole.AlternateBase: SURFACE,
        QPalette.ColorRole.ToolTipBase: SURFACE_ALT,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.PlaceholderText: "#6a6a88",
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE_ALT,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: ACCENT_HOVER,
        QPalette.ColorRole.LinkVisited: "#9a8ae8",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#5a5a78"))
    return p


def apply_modern_style(app):
    """Apply the Fusion style, dark palette and shared stylesheet to ``app``."""
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(STYLESHEET)
