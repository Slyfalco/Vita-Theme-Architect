import os
from widgets import download_progress_text
import time
import datetime
import zipfile

import requests
from urllib.parse import urlparse

from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QFrame, QLineEdit,
    QComboBox, QPushButton, QListWidget, QListWidgetItem,
    QLabel, QFormLayout, QScrollArea, QWidget,
    QProgressDialog, QMessageBox, QApplication, QSpinBox,
    QRadioButton, QButtonGroup, QLayout
)
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath
from PyQt6.QtCore import Qt, QSize, QUrl, QTimer, QRectF
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply, QSslSocket

from constants import crypted, secure_url, THEME_PAGE


class ThemeDownloaderDialog(QDialog):
    def __init__(self, main_window, parent=None):
        super().__init__(parent); self.main_window = main_window; self.setWindowTitle("🌐 Download Themes"); self.resize(1000, 700)
        self.selected_theme_data = None; self.page = 1; self.pages = 1; layout = QHBoxLayout(self)
        left_panel = QFrame(); left_layout = QVBoxLayout()
        search_layout = QHBoxLayout(); self.downloader_search_bar = QLineEdit(); self.downloader_search_bar.setPlaceholderText("Search..."); self.downloader_search_bar.textChanged.connect(self.on_search_typed)
        # The list is searched page by page: wait for a pause in typing.
        self.search_timer = QTimer(self); self.search_timer.setSingleShot(True); self.search_timer.setInterval(300); self.search_timer.timeout.connect(self.filter_and_display_themes)
        self.downloader_search_combo = QComboBox(); self.downloader_search_combo.addItems(["Search by Title", "Search by Author", "Search by Tags"]); self.downloader_search_combo.currentIndexChanged.connect(self.filter_and_display_themes)
        self.clear_filter_button = QPushButton("Clear"); self.clear_filter_button.clicked.connect(lambda: self.downloader_search_bar.setText("")); self.clear_filter_button.setVisible(False)
        search_layout.addWidget(self.downloader_search_bar); search_layout.addWidget(self.downloader_search_combo); search_layout.addWidget(self.clear_filter_button); left_layout.addLayout(search_layout)
        sort_layout = QHBoxLayout(); sort_layout.addWidget(QLabel("Sort by:"))
        self.downloader_sort_combo = QComboBox(); self.downloader_sort_combo.addItems(["Last Updated (Newest)", "Last Updated (Oldest)", "Alphabetical (A-Z)", "Alphabetical (Z-A)"]); self.downloader_sort_combo.currentIndexChanged.connect(self.filter_and_display_themes)
        sort_layout.addWidget(self.downloader_sort_combo); sort_layout.addStretch()
        # Theme count sits up here by the sort, not crammed among the paging buttons.
        self.page_label = QLabel(); self.page_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sort_layout.addWidget(self.page_label); left_layout.addLayout(sort_layout)
        self.theme_list_widget = QListWidget(); self.theme_list_widget.itemClicked.connect(self.on_theme_selected); left_layout.addWidget(self.theme_list_widget)
        # Pages of THEME_PAGE themes. Previous/Next, a numbered pager
        # (1 2 3 … N, click to jump), a type-a-page box, and Refresh.
        self.prev_button = QPushButton("◀ Previous"); self.prev_button.setToolTip("The previous page of themes."); self.prev_button.clicked.connect(lambda: self.show_page(self.page - 1))
        self.next_button = QPushButton("Next ▶"); self.next_button.setToolTip("The next page of themes."); self.next_button.clicked.connect(lambda: self.show_page(self.page + 1))
        self.pager_widget = QWidget(); self.pager_widget.setMinimumWidth(240)   # fixed footprint: the cluster doesn't resize as pages change
        self.pager_row = QHBoxLayout(self.pager_widget); self.pager_row.setContentsMargins(0, 0, 0, 0); self.pager_row.setSpacing(3)
        pager_line = QHBoxLayout()
        pager_line.addWidget(self.prev_button); pager_line.addStretch()
        pager_line.addWidget(self.pager_widget)
        pager_line.addStretch(); pager_line.addWidget(self.next_button)
        left_layout.addLayout(pager_line)
        # Go-to-page on the left, Reload on the right, well apart.
        go_line = QHBoxLayout()
        self.page_spin = QSpinBox(); self.page_spin.setMinimum(1); self.page_spin.setMaximum(1)
        self.page_spin.setToolTip("Type a page number, then press Enter or Go.")
        self.page_spin.lineEdit().returnPressed.connect(lambda: self.show_page(self.page_spin.value()))
        go_button = QPushButton("Go"); go_button.setToolTip("Jump to the typed page."); go_button.clicked.connect(lambda: self.show_page(self.page_spin.value()))
        self.refresh_button = QPushButton("Reload"); self.refresh_button.setToolTip("Reload the latest theme list from the repository (new and updated themes) and return to page 1.")
        self.refresh_button.clicked.connect(lambda: self.show_page(1, fresh=True))
        go_line.addWidget(QLabel("Go to page:")); go_line.addWidget(self.page_spin); go_line.addWidget(go_button)
        go_line.addStretch(); go_line.addWidget(self.refresh_button)
        left_layout.addLayout(go_line); left_panel.setLayout(left_layout)
        right_panel = QFrame(); right_panel.setFrameShape(QFrame.Shape.StyledPanel); right_panel.setProperty("role", "panel"); right_layout = QVBoxLayout()
        preview_layout = QHBoxLayout()
        self.home_preview_label = QLabel("Click a theme\nto see its preview"); self.home_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.home_preview_label.setFixedSize(QSize(282, 158)); self.home_preview_label.setProperty("role", "preview")
        self.lock_preview_label = QLabel("Click a theme\nto see its preview"); self.lock_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lock_preview_label.setFixedSize(QSize(282, 158)); self.lock_preview_label.setProperty("role", "preview")
        preview_layout.addWidget(self.home_preview_label); preview_layout.addWidget(self.lock_preview_label); right_layout.addLayout(preview_layout)
        info_scroll = QScrollArea(); info_scroll.setWidgetResizable(True); info_widget = QWidget(); self.info_layout = QFormLayout(); info_widget.setLayout(self.info_layout) # Made info_layout an attribute
        self.title_label = QLabel(); self.author_label = QLabel(); self.version_label = QLabel(); self.downloads_label = QLabel(); self.date_added_label = QLabel(); self.last_updated_label = QLabel(); self.validation_label = QLabel()
        self.tags_widget = QWidget(); self.tags_layout = QHBoxLayout(self.tags_widget); self.tags_layout.setContentsMargins(0,0,0,0)
        self.description_label = QLabel(); self.description_label.setWordWrap(True)
        self.description_row_label = QLabel("<b>Description:</b>") # Store label reference
        self.info_layout.addRow("<b>Title:</b>", self.title_label); self.info_layout.addRow("<b>Author:</b>", self.author_label); self.info_layout.addRow("<b>Version:</b>", self.version_label); self.info_layout.addRow("<b>Downloads:</b>", self.downloads_label); self.info_layout.addRow("<b>Date Added:</b>", self.date_added_label); self.info_layout.addRow("<b>Last Updated:</b>", self.last_updated_label); self.info_layout.addRow("<b>Validation:</b>", self.validation_label); self.info_layout.addRow("<b>Tags:</b>", self.tags_widget); self.info_layout.addRow(self.description_row_label, self.description_label)
        info_scroll.setWidget(info_widget); right_layout.addWidget(info_scroll)
        self.extra_button = QPushButton("Open/Download Extra"); self.extra_button.setProperty("role", "primary"); self.extra_button.clicked.connect(self.handle_extra_download); self.extra_button.setVisible(False); right_layout.addWidget(self.extra_button)
        self.download_button = QPushButton("Download && Import Theme"); self.download_button.setProperty("role", "success"); self.download_button.setEnabled(False); self.download_button.clicked.connect(self.download_selected_theme); right_layout.addWidget(self.download_button)
        right_panel.setLayout(right_layout); layout.addWidget(left_panel, 1); layout.addWidget(right_panel, 1)
        self.home_net_manager = QNetworkAccessManager(self); self.home_net_manager.finished.connect(self.on_home_preview_loaded); self.lock_net_manager = QNetworkAccessManager(self); self.lock_net_manager.finished.connect(self.on_lock_preview_loaded); self.fetch_themes()
    def showEvent(self, event):
        super().showEvent(event)
        from widgets import center_on_parent
        center_on_parent(self)
    SEARCH_FIELDS = {"Search by Title": 'title', "Search by Author": 'author', "Search by Tags": 'tags'}
    SORTS = {"Last Updated (Newest)": 'newest', "Last Updated (Oldest)": 'oldest', "Alphabetical (A-Z)": 'az', "Alphabetical (Z-A)": 'za'}
    def fetch_themes(self):
        # Opening the downloader always gets the live list.
        self.show_page(1, fresh=True, closing_on_error=True)
    def on_search_typed(self, text):
        self.clear_filter_button.setVisible(bool(text)); self.search_timer.start()
    def filter_and_display_themes(self):
        # A new search or sort starts at its first page.
        self.show_page(1)
    def show_page(self, page, fresh=False, closing_on_error=False):
        """Show one page of the online list for the current search and sort
        (searching and sorting happen in the list itself, a page at a time)."""
        search = self.downloader_search_bar.text().strip(); field = self.SEARCH_FIELDS.get(self.downloader_search_combo.currentText(), 'title')
        sort = self.SORTS.get(self.downloader_sort_combo.currentText(), 'newest')
        try:
            total = crypted.count(search, field, fresh)
            if total is None: raise AttributeError
            self.pages = max(1, -(-total // THEME_PAGE)); self.page = min(max(1, page), self.pages)
            themes = crypted.get(search, field, sort, self.page)
            if themes is None: raise AttributeError
        except AttributeError:
            QMessageBox.warning(self, "Error", "Conditions not met.")
            QTimer.singleShot(1, self.close); return
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not fetch theme list:\n{e}")
            if closing_on_error: QTimer.singleShot(1, self.close)
            return
        self.theme_list_widget.clear()
        for theme in themes: item = QListWidgetItem(f"{theme['title']}\nby {theme['author']}"); item.setData(Qt.ItemDataRole.UserRole, theme); self.theme_list_widget.addItem(item)
        self.theme_list_widget.scrollToTop()
        self.page_label.setText(f"{total:,} theme{'s' if total != 1 else ''}" if total else "No themes found")
        self.page_spin.setMaximum(self.pages); self.page_spin.setEnabled(self.pages > 1)
        self.page_spin.blockSignals(True); self.page_spin.setValue(self.page); self.page_spin.blockSignals(False)
        self._rebuild_pager()
        self.prev_button.setEnabled(self.page > 1); self.next_button.setEnabled(self.page < self.pages)

    @staticmethod
    def _page_numbers(current, total):
        """Page numbers to show, with None marking an ellipsis gap: always the
        first and last page and the current one with a neighbour either side,
        e.g. 1 … 4 5 6 … 77."""
        wanted = sorted({1, total, current - 1, current, current + 1})
        out, prev = [], 0
        for p in wanted:
            if 1 <= p <= total:
                if prev and p - prev > 1:
                    out.append(None)
                out.append(p); prev = p
        return out

    def _rebuild_pager(self):
        """Rebuild the numbered pager for the current page."""
        while self.pager_row.count():
            item = self.pager_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.pager_row.addStretch()   # centre the cluster within the fixed footprint
        for p in self._page_numbers(self.page, self.pages):
            if p is None:
                gap = QLabel("…"); gap.setAlignment(Qt.AlignmentFlag.AlignCenter); gap.setFixedWidth(28)
                self.pager_row.addWidget(gap)
            else:
                # Small clickable box; same size for every number so spacing
                # stays even. The current page is filled (accent), not clickable.
                button = QPushButton(str(p)); button.setMinimumWidth(28); button.setMaximumWidth(52)
                button.setProperty("role", "page")
                if p == self.page:
                    button.setProperty("current", True)   # filled accent box, stays put
                else:
                    button.setCursor(Qt.CursorShape.PointingHandCursor)
                    button.clicked.connect(lambda _checked=False, n=p: self.show_page(n))
                self.pager_row.addWidget(button)
        self.pager_row.addStretch()
    def convert_epoch_to_str(self, epoch_time):
        try: return datetime.datetime.fromtimestamp(int(epoch_time)).strftime('%d %B %Y')
        except Exception: return str(epoch_time)
    def on_theme_selected(self, item):
        self.selected_theme_data = item.data(Qt.ItemDataRole.UserRole)
        self.title_label.setText(self.selected_theme_data['title'])
        self.author_label.setText(self.selected_theme_data['author'])
        self.version_label.setText(self.selected_theme_data.get('version', 'N/A'))
        self.downloads_label.setText(self.selected_theme_data.get('downloads', 'N/A'))
        self.date_added_label.setText(self.convert_epoch_to_str(self.selected_theme_data.get('uid', 'N/A')))
        self.last_updated_label.setText(self.convert_epoch_to_str(self.selected_theme_data.get('id', 'N/A')))

        warnings = self.selected_theme_data.get('warnings', '0')
        errors = self.selected_theme_data.get('errors', '0')

        self.validation_label.setText(f"{warnings} Warnings, {errors} Errors")
        # --- Handle Tags/Description ---
        while self.tags_layout.count():
            widget = self.tags_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            del widget
        tags_string = self.selected_theme_data.get('tags', '')
        description_string = self.selected_theme_data.get('description', '')
        extracted_tags = []

        # Check description for comma-separated tags only if 'tags' is empty
        if ',' in description_string and not tags_string:
             extracted_tags = [t.strip() for t in description_string.split(',') if t.strip()]
             # If description was used as tags, clear the description display
             self.description_label.setText("")
             self.description_label.setVisible(False)
             self.description_row_label.setVisible(False)
        elif tags_string:
             extracted_tags = [t.strip() for t in tags_string.split(',') if t.strip()]
             # If tags field used, display the original description
             self.description_label.setText(description_string if description_string else 'No description provided.')
             self.description_label.setVisible(True)
             self.description_row_label.setVisible(True)
        else:
             # No tags found anywhere, show description
             self.description_label.setText(description_string if description_string else 'No description provided.')
             self.description_label.setVisible(True)
             self.description_row_label.setVisible(True)

        # Populate tags widget if tags were found
        if extracted_tags:
            for tag in extracted_tags:
                tag_button = QPushButton(tag); tag_button.setProperty("role", "link"); tag_button.setCursor(Qt.CursorShape.PointingHandCursor)
                tag_button.clicked.connect(lambda ch, t=tag: self.filter_by_tag(t)); self.tags_layout.addWidget(tag_button)
            self.tags_layout.addStretch()
        else:
            self.tags_layout.addWidget(QLabel("None"))
            self.tags_layout.addStretch()
        # --- End Handle Tags/Description ---

        # Previews load through Qt: https when Qt has TLS support (on Linux it
        # needs the system's OpenSSL), else as listed so they still show.
        preview_url = secure_url if QSslSocket.supportsSsl() else (lambda u: u)
        home_preview_url = preview_url(self.selected_theme_data.get('home_preview'));
        if home_preview_url: self.home_preview_label.setText("Loading preview..."); request = QNetworkRequest(QUrl(home_preview_url)); self.home_net_manager.get(request)
        else: self.home_preview_label.setText("No Home preview")
        lock_preview_url = preview_url(self.selected_theme_data.get('lockscreen_preview'));
        if lock_preview_url: self.lock_preview_label.setText("Loading preview..."); request = QNetworkRequest(QUrl(lock_preview_url)); self.lock_net_manager.get(request)
        else: self.lock_preview_label.setText("No Lock preview")
        self.extra_button.setVisible(bool(self.selected_theme_data.get('extra'))); self.download_button.setEnabled(True)
    def filter_by_tag(self, tag): self.downloader_search_combo.setCurrentText("Search by Tags"); self.downloader_search_bar.setText(tag)
    def on_home_preview_loaded(self, reply): self._load_preview_to_label(reply, self.home_preview_label, "Home preview")
    def on_lock_preview_loaded(self, reply): self._load_preview_to_label(reply, self.lock_preview_label, "Lock preview")
    def _load_preview_to_label(self, reply, label, error_name):
        if reply.error() == QNetworkReply.NetworkError.NoError:
            pixmap = QPixmap(); pixmap.loadFromData(reply.readAll())
            label.setPixmap(self._rounded(pixmap, label.size(), 8))
        else:
            label.setText(f"{error_name} failed to load")
        reply.deleteLater()

    @staticmethod
    def _rounded(pixmap, size, radius):
        """Fill the box with the preview (keeping aspect, cropping the overflow
        so there are no uneven side gaps) and round the corners to the box."""
        target = QSize(size.width() - 2, size.height() - 2)   # just inside the 1px border
        scaled = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
        # Centre-crop the overflow so the image fills the box evenly.
        x = (scaled.width() - target.width()) // 2
        y = (scaled.height() - target.height()) // 2
        scaled = scaled.copy(x, y, target.width(), target.height())
        rounded = QPixmap(target); rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, target.width(), target.height()), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        return rounded
    def download_selected_theme(self):
        if not self.selected_theme_data:
            return
        zip_url = self.selected_theme_data['zip']
        title = self.selected_theme_data['title']
        self._download_file(zip_url, title, self.on_theme_download_complete, f"Downloading '{title}'...")
    def on_theme_download_complete(self, temp_path):
        self.main_window.import_zip_from_path(temp_path, fromDownload=True)
        #  self.accept()
    def handle_extra_download(self):
        """
        Handles the click of the 'Open/Download Extra' button.
        This correctly executes only if theme data and an 'extra' URL exist.
        """
        # We check if the data *does* exist
        if self.selected_theme_data and self.selected_theme_data.get('extra'):
            # If it does, we set the variables and start the download.
            extra_value = self.selected_theme_data['extra']

            parsed = urlparse(extra_value)
            if not (parsed.scheme and parsed.netloc):
                # If it's all numbers, construct the new URL
                self.extra_url = f"https://psvt.ovh/{extra_value}"
            else:
                # Otherwise, use the value as the URL (original behavior)
                self.extra_url = extra_value

            self.title = self.selected_theme_data['title']

            # This was the code that was previously unreachable
            self._download_file(
                self.extra_url,
                f"{self.title}_extra",
                self.on_extra_download_complete,
                f"Downloading extra for '{self.title}'..."
            )

        # If the 'if' check fails, the function simply does nothing,
        # which is the correct behavior.
    def on_extra_download_complete(self, temp_path):
        """
        Called after the 'extra' file is downloaded.
        This now validates the file as a .zip and extracts its contents.
        """
        extras_path = self.main_window.extras_library_path

        # Create the target folder based on the theme title
        safe_folder_name = "".join(c for c in self.title if c.isalnum() or c in (' ', '_')).rstrip()
        target_folder = os.path.join(extras_path, safe_folder_name)
        os.makedirs(target_folder, exist_ok=True)

        try:
            # Check if the downloaded file is a valid zip file
            if zipfile.is_zipfile(temp_path):
                # If it is, extract all its contents to the target folder
                with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                    zip_ref.extractall(target_folder)

                QMessageBox.information(self, "Extra Downloaded",
                                      f"Extra files for '{self.title}' extracted successfully to:\n{target_folder}")
            else:
                # If it's not a zip, report an error.
                # This will catch cases where a MediaFire link downloaded an HTML page instead of a file.
                QMessageBox.warning(self, "Download Error",
                                  "The downloaded 'extra' file was not a valid .zip file and could not be extracted.")

        except zipfile.BadZipFile:
            QMessageBox.critical(self, "Extraction Error",
                                 "Failed to extract the downloaded file. It may be corrupt or not a valid zip archive.")
        except Exception as e:
            QMessageBox.critical(self, "Extraction Error", f"An error occurred during extraction: {e}")
    def _download_file(self, url, title, callback_on_success, progress_title):
        safe_filename = "".join(c for c in title if c.isalnum() or c in (' ', '_')).rstrip() + ".download"; self.temp_path = os.path.join(os.path.abspath('.'), safe_filename)
        try:
            self.progress_dialog = QProgressDialog(progress_title, "Cancel", 0, 0, self); self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
            response = requests.get(secure_url(url), stream=True, timeout=30); response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0)); self.progress_dialog.setMaximum(total_size); downloaded_size = 0
            # Size, speed and time left, refreshed a few times a second (not per chunk).
            start = last_text = time.monotonic()
            with open(self.temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=65536):
                    f.write(chunk); downloaded_size += len(chunk); self.progress_dialog.setValue(downloaded_size)
                    now = time.monotonic()
                    if now - last_text >= 0.25:
                        last_text = now
                        speed = downloaded_size / (now - start) if now - start > 0.5 else None
                        self.progress_dialog.setLabelText(download_progress_text(progress_title, downloaded_size, total_size, speed))
                    QApplication.processEvents()
                    if self.progress_dialog.wasCanceled(): f.close(); os.remove(self.temp_path); return
            self.progress_dialog.close(); callback_on_success(self.temp_path)
            if os.path.exists(self.temp_path): os.remove(self.temp_path)
        except Exception as e:
            if hasattr(self, 'progress_dialog'): self.progress_dialog.close()
            if hasattr(self, 'temp_path') and os.path.exists(self.temp_path): os.remove(self.temp_path)
            QMessageBox.critical(self, "Error", f"Could not download file:\n{e}")


class ThemeExistsDialog(QDialog):
    """Shown on Submit when a theme with this name already exists: it shows the
    matching theme(s) with their previews (like the downloader) so the user can
    see what they'd update, then choose Update Existing / Submit as New / Cancel.
    With several matches the user picks which one to update.

    After exec(): `action` is 'update', 'new' or 'cancel'; `selected_theme` is
    the match chosen for an update."""

    def __init__(self, parent, theme_title, matches):
        super().__init__(parent)
        self.setWindowTitle("Theme Exists")
        self.action = 'cancel'
        self.selected_theme = matches[0] if matches else None
        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._preview_loaded)
        self._pending = {}   # reply -> preview label

        layout = QVBoxLayout(self)
        intro = QLabel(f"A theme named <b>&apos;{theme_title}&apos;</b> already exists in the repository.")
        intro.setWordWrap(True); layout.addWidget(intro)
        layout.addWidget(QLabel("Update one of the existing themes below, or submit this as a completely new theme."
                                if len(matches) > 1 else
                                "Update the existing theme below, or submit this as a completely new theme."))

        self._group = QButtonGroup(self)
        preview_url = secure_url if QSslSocket.supportsSsl() else (lambda u: u)
        cards = QWidget(); cards_v = QVBoxLayout(cards); cards_v.setContentsMargins(0, 0, 0, 0); cards_v.setSpacing(8)
        for i, theme in enumerate(matches):
            cards_v.addWidget(self._make_card(i, theme, len(matches) > 1, preview_url))

        if len(matches) > 1:
            # Several matches: scroll, capped so the dialog doesn't get too tall.
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(cards); scroll.setMaximumHeight(430)
            layout.addWidget(scroll)
        else:
            layout.addWidget(cards)   # single match: no scroll, hug the one card

        buttons = QHBoxLayout(); buttons.addStretch()
        btn_update = QPushButton("Update Existing"); btn_update.setProperty("role", "success")
        btn_update.clicked.connect(lambda: self._finish('update'))
        btn_new = QPushButton("Submit as New"); btn_new.clicked.connect(lambda: self._finish('new'))
        btn_cancel = QPushButton("Cancel"); btn_cancel.clicked.connect(self.reject)
        for b in (btn_update, btn_new, btn_cancel):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        # Size to the content so there's no empty gap around the card(s).
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.setMinimumWidth(560)

    def showEvent(self, event):
        super().showEvent(event)
        from widgets import center_on_parent
        center_on_parent(self)

    def _make_card(self, i, theme, multi, preview_url):
        card = QFrame(); card.setProperty("role", "panel")
        row = QHBoxLayout(card)
        if multi:
            radio = QRadioButton(); radio.setChecked(i == 0)
            radio.toggled.connect(lambda on, t=theme: self._select(t) if on else None)
            self._group.addButton(radio); row.addWidget(radio)
        preview = QLabel("Loading preview…"); preview.setFixedSize(QSize(213, 120))
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter); preview.setProperty("role", "preview")
        row.addWidget(preview)
        info = QVBoxLayout()
        info.addWidget(QLabel(f"<b>{theme.get('title', '')}</b>"))
        info.addWidget(QLabel(f"by {theme.get('author', '?')}"))
        info.addWidget(QLabel(f"Version {theme.get('version', '?')}  ·  {theme.get('downloads', '0')} downloads"))
        info.addStretch()
        row.addLayout(info, 1)
        url = preview_url(theme.get('home_preview') or theme.get('lockscreen_preview'))
        if url:
            self._pending[self._net.get(QNetworkRequest(QUrl(url)))] = preview
        else:
            preview.setText("No preview")
        return card

    def _select(self, theme):
        self.selected_theme = theme

    def _finish(self, action):
        self.action = action
        self.accept()

    def _preview_loaded(self, reply):
        label = self._pending.pop(reply, None)
        if label is not None:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                pixmap = QPixmap(); pixmap.loadFromData(reply.readAll())
                if not pixmap.isNull():
                    label.setPixmap(ThemeDownloaderDialog._rounded(pixmap, label.size(), 8))
                else:
                    label.setText("No preview")
            else:
                label.setText("Preview unavailable")
        reply.deleteLater()
