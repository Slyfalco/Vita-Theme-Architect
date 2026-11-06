from PyQt6.QtWidgets import QPushButton, QColorDialog, QLineEdit
from PyQt6.QtGui import QIcon, QColor, QPixmap
from PyQt6.QtCore import QSize, Qt


def download_progress_text(label, done, total, speed):
    """'label: 1.2 GB of 2.5 GB, 18 MB/s, 1 min 10 s left' for any download
    (speed from the bytes already received, so it costs the download nothing)."""
    def size(n):
        return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB" if n >= 1024 ** 2 else f"{n / 1024:.0f} KB"
    text = f"{label}: {size(done)}" + (f" of {size(total)}" if total else "")
    if speed:
        text += f", {size(speed)}/s"
        if total and total > done:
            left = int(round((total - done) / speed))
            text += (f", {left // 60} min {left % 60:02d} s left" if left >= 60 else f", {left} s left")
    return text


def center_on_parent(dialog):
    """Centre a dialog over its parent window, then clamp it to the screen's
    available area so it never runs off the edges (particularly past the bottom
    when the parent is maximised). Centres on the screen if there's no parent.

    Uses frameGeometry (which includes the title bar) so the dialog doesn't
    drift downward the way centring on the client-area geometry() does.
    """
    from PyQt6.QtWidgets import QApplication
    frame = dialog.frameGeometry()
    parent = dialog.parent() if hasattr(dialog, 'parent') else None
    if parent is not None and parent.isVisible():
        frame.moveCenter(parent.frameGeometry().center())
    else:
        screen = dialog.screen() or QApplication.primaryScreen()
        if screen is not None:
            frame.moveCenter(screen.availableGeometry().center())

    screen = dialog.screen() or QApplication.primaryScreen()
    if screen is not None:
        # Keep a small margin from the screen edges so dialogs never hug the top
        # and bottom (a window as tall as the screen looked cramped).
        margin = 24
        avail = screen.availableGeometry().adjusted(margin, margin, -margin, -margin)
        # Shrink an over-tall/wide dialog to fit, then nudge it fully on-screen.
        if frame.height() > avail.height():
            dialog.resize(dialog.width(), avail.height() - (frame.height() - dialog.height()))
            frame = dialog.frameGeometry()
            frame.moveCenter(avail.center())
        if frame.width() > avail.width():
            dialog.resize(avail.width() - (frame.width() - dialog.width()), dialog.height())
            frame = dialog.frameGeometry()
            frame.moveCenter(avail.center())
        if frame.right() > avail.right():
            frame.moveRight(avail.right())
        if frame.left() < avail.left():
            frame.moveLeft(avail.left())
        if frame.bottom() > avail.bottom():
            frame.moveBottom(avail.bottom())
        if frame.top() < avail.top():
            frame.moveTop(avail.top())

    dialog.move(frame.topLeft())


def play_done_sound(window=None):
    """Let the user know a long task finished (icon generation, saving a
    theme): a short chime (Assets/Sounds/done.wav; the system ping was easy
    to miss), plus a flashing taskbar/dock icon when the app isn't in front."""
    import os
    import platform
    import subprocess
    from PyQt6.QtWidgets import QApplication
    if window is not None:
        QApplication.alert(window, 0)
    chime = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Assets', 'Sounds', 'done.wav')
    if os.path.isfile(chime):
        try:
            system = platform.system()
            if system == 'Windows':
                import winsound
                winsound.PlaySound(chime, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            players = [['afplay', chime]] if system == 'Darwin' else [['paplay', chime], ['aplay', '-q', chime]]
            for cmd in players:
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    continue
        except Exception:
            pass
    QApplication.beep()


class DragDropLineEdit(QLineEdit):
    """A QLineEdit that accepts drag-and-drop of a single file path.

    Highlights with a dashed blue border while a file is dragged over it.
    On drop, sets the line edit text to the dropped file's path.
    An optional ``file_filter`` list (e.g. ['.exe']) restricts accepted types.
    """

    _NORMAL_STYLE = ""
    _HOVER_STYLE = "border: 2px dashed #4a90d9; border-radius: 6px; background-color: #1e3a5f;"

    def __init__(self, file_filter=None, parent=None):
        super().__init__(parent)
        self._file_filter = [f.lower() for f in (file_filter or [])]
        self.setAcceptDrops(True)

    def _accepts_path(self, path):
        if not self._file_filter:
            return True
        _, ext = __import__('os').path.splitext(path)
        return ext.lower() in self._file_filter

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls()]
            if paths and self._accepts_path(paths[0]):
                event.acceptProposedAction()
                self.setStyleSheet(self._HOVER_STYLE)
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self._NORMAL_STYLE)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.setStyleSheet(self._NORMAL_STYLE)
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls()]
            if paths and self._accepts_path(paths[0]):
                self.setText(paths[0])
                event.acceptProposedAction()
                return
        event.ignore()


class ColorPickerButton(QPushButton):
    def __init__(self, default_color='#ffffffff', parent=None):
        super().__init__(parent); self.setFixedSize(QSize(100, 25)); self.color_hex = default_color; self.update_color(); self.clicked.connect(self.pick_color)
    def pick_color(self):
        hex_val = self.color_hex.lstrip('#'); alpha = int(hex_val[0:2], 16); rgb = f"#{hex_val[2:]}"; color = QColor(rgb); color.setAlpha(alpha); new_color = QColorDialog.getColor(color, self, "Select Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if new_color.isValid(): self.color_hex = f"{new_color.alpha():02x}{new_color.name(QColor.NameFormat.HexRgb).lstrip('#')}"; self.update_color()
    def update_color(self): pixmap = QPixmap(self.size()); pixmap.fill(QColor(f"#{self.color_hex[2:]}")); self.setIcon(QIcon(pixmap)); self.setText(f"#{self.color_hex}")
    def get_color(self): return self.color_hex
