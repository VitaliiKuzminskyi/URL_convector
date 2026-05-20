import sys
import os
import re
import json
import time
import urllib.parse
import urllib.request
import urllib.error

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QMessageBox,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QStyledItemDelegate, QPlainTextEdit,
)
from PySide6.QtGui import QFont, QIcon, QPixmap, QMovie, QPainter, QColor, QKeySequence, QTextOption
from PySide6.QtCore import (
    Qt, Signal, QObject, QThread, QTimer,
    QRect, QPropertyAnimation, QEasingCurve,
)


# ============================================================
# Helpers
# ============================================================

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def parse_multipart(body: str) -> dict:
    """Parse multipart/form-data body (matches name="..."\\r\\n\\r\\nvalue\\r\\n)."""
    matches = re.findall(
        r'name="([^"]+)"\\r\\n\\r\\n(.*?)\\r\\n',
        body,
        re.DOTALL,
    )
    return {key: value.strip() for key, value in matches}


def parse_url_encoded(body: str) -> dict:
    """Parse application/x-www-form-urlencoded body like 'a=1&b=2'."""
    return dict(urllib.parse.parse_qsl(body, keep_blank_values=True))


def parse_body(body: str) -> dict:
    """Auto-detect body format: try multipart first, fall back to URL-encoded."""
    params = parse_multipart(body)
    if params:
        return params
    return parse_url_encoded(body)


def convert_to_url(text: str) -> str:
    text = text.strip()

    url_match = re.search(r"curl '([^']+)'", text)
    if not url_match:
        raise ValueError("It's not cURL (bash) type URL.\n\nPaste cURL (bash) type URL from DevTools please.")

    base_url = url_match.group(1)

    data_match = re.search(r"--data-raw \$'(.*?)'", text, re.DOTALL)
    if not data_match:
        data_match = re.search(r"--data-raw '(.*?)'", text, re.DOTALL)

    params = parse_body(data_match.group(1)) if data_match else {}
    query = urllib.parse.urlencode(params)

    if not query:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{query}"


def split_url(url: str) -> tuple:
    """Split URL into (base_url_with_trailing_?, params_dict).
    base_url keeps trailing '?' if there were any params or the URL ended with '?'."""
    url = url.strip()
    if "?" in url:
        base, query = url.split("?", 1)
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        return base + "?", params
    return url, {}


def build_url(base: str, params: dict) -> str:
    """Build URL from base (may end with '?') + params dict."""
    base = base.rstrip("?")
    if not params:
        return base
    query = urllib.parse.urlencode(params)
    return f"{base}?{query}"


# ============================================================
# Custom widgets
# ============================================================

class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event):
        self.clicked.emit()


class GifOverlay(QWidget):

    def __init__(self, gif_path, parent):
        super().__init__(parent)
        self.setGeometry(parent.rect())
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.movie = QMovie(gif_path)

        self.gif_label = QLabel(self)
        self.gif_label.setStyleSheet("background: transparent;")
        self.gif_label.setMovie(self.movie)
        self.movie.frameChanged.connect(self._on_first_frame)
        self.movie.start()

        self.show()
        self.raise_()
        self.gif_label.show()

    def _on_first_frame(self):
        size = self.movie.currentPixmap().size()
        if size.isEmpty():
            return
        max_w, max_h = self.width() - 20, self.height() - 20
        if size.width() > max_w or size.height() > max_h:
            size = size.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio)
            self.movie.setScaledSize(size)
        self.gif_label.resize(size)
        self.gif_label.move(
            (self.width() - size.width()) // 2,
            (self.height() - size.height()) // 2,
        )
        self.movie.frameChanged.disconnect(self._on_first_frame)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 150))

    def mousePressEvent(self, event):
        self.movie.stop()
        self.deleteLater()


class JsonResponseEdit(QTextEdit):
    """Read-only monospace text view for response body.
    Long lines are NOT wrapped — horizontal scroll appears as needed,
    keeping JSON in its natural single-line-per-element style."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 10))
        # JSON style: don't wrap long lines; let the user scroll horizontally
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)


class ExpandableLineEdit(QPlainTextEdit):
    """A QPlainTextEdit styled to look like a single-line QLineEdit by default
    (fixed 1-line height, no wrap, horizontal scroll). On double-click it
    expands vertically to fit the full text with word-wrap-anywhere (so long
    URL-encoded values stay readable). Focus-out collapses it back. Enter and
    Esc both commit (clearFocus) and don't insert newlines.

    Exposes `.text()` / `.setText()` aliases so call sites built for QLineEdit
    work without changes."""

    DEFAULT_HEIGHT = 34  # 1 line + padding for Consolas 10pt
    MAX_EXPANDED_HEIGHT = 240  # cap so a giant URL doesn't dominate the panel

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Consolas", 10))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # No horizontal scrollbar in collapsed state — overflow is silently
        # clipped on the right; user double-clicks to expand & see the rest.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setFixedHeight(self.DEFAULT_HEIGHT)
        self._expanded = False

    # --- QLineEdit-compatible aliases (so existing code keeps working) ---
    def text(self) -> str:
        return self.toPlainText()

    def setText(self, txt: str) -> None:
        # Avoid spurious textChanged when value is unchanged
        if self.toPlainText() != txt:
            self.setPlainText(txt)

    # --- expand on double-click, collapse on focus-out ---
    def mouseDoubleClickEvent(self, event):
        self._expand()
        super().mouseDoubleClickEvent(event)

    def focusOutEvent(self, event):
        self._collapse()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        # Enter / Esc — commit (no newline insertion)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            event.accept()
            self.clearFocus()
            return
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.clearFocus()
            return
        super().keyPressEvent(event)

    def _expand(self):
        if self._expanded:
            return
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        fm = self.fontMetrics()
        text = self.toPlainText()
        w = max(self.viewport().width() - 10, 50)
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.TextFlag.TextWrapAnywhere)
        rect = fm.boundingRect(0, 0, w, 10000, flags, text)
        needed = min(rect.height() + 18, self.MAX_EXPANDED_HEIGHT)
        self.setFixedHeight(max(self.DEFAULT_HEIGHT, needed))
        self._expanded = True

    def _collapse(self):
        if not self._expanded:
            return
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(self.DEFAULT_HEIGHT)
        self._expanded = False


class MultiLineEditDelegate(QStyledItemDelegate):
    """Editor delegate that uses a word-wrapping QPlainTextEdit instead of
    the default single-line QLineEdit. Enter inserts a newline, Tab/Esc/click-out
    commits or cancels the edit."""

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setTabChangesFocus(True)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        # Wrap on word boundary if possible, else break inside the word.
        # Critical for unbreakable strings like URL-encoded values.
        editor.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        editor.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        return editor

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data(Qt.ItemDataRole.EditRole) or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class ParamsTable(QTableWidget):
    """Two-column key/value editor with these behaviors:
    - Key column width = max(longest key + padding, capped at 50% of table width)
    - Default row height is fixed; long values are truncated with ellipsis.
    - SINGLE click: row size doesn't change. The cell is selected (subtle grey).
      Ctrl+C / Ctrl+X / Ctrl+V act on the selected cell's text directly.
    - DOUBLE click: row expands vertically and enters edit mode with word-wrap.
    - Losing focus or finishing the edit collapses the row back."""

    DEFAULT_ROW_HEIGHT = 28
    KEY_COLUMN_PADDING = 24
    KEY_COLUMN_FALLBACK = 80

    # Subtle grey selection — no blue, no bold header
    _STYLE_SHEET = """
        QTableWidget {
            selection-background-color: #e8e8e8;
            selection-color: black;
        }
        QTableWidget::item:selected {
            background-color: #e8e8e8;
            color: black;
        }
        QTableWidget::item:selected:!active {
            background-color: #e8e8e8;
            color: black;
        }
        QHeaderView::section {
            font-weight: normal;
        }
    """

    def __init__(self, parent=None):
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(["Key", "Value"])
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(self.DEFAULT_ROW_HEIGHT)
        self.setWordWrap(True)
        self.setAlternatingRowColors(True)

        h = self.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # Don't bold the column header when its column has a selected cell
        h.setHighlightSections(False)

        self.setStyleSheet(self._STYLE_SHEET)

        # Word-wrap multi-line editor on double-click
        self.setItemDelegate(MultiLineEditDelegate(self))

        # Only double-click triggers edit (the default); single click only selects.
        # We don't trigger edit on AnyKeyPressed because we want Ctrl+C/V on the
        # selected cell to act as a whole-value op, not start typing inside it.
        self.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
        )

        self._expanded_row = -1

    def fill(self, params: dict):
        """Populate the table from a dict, resetting row heights and column widths."""
        self.setRowCount(0)
        for key, value in params.items():
            row = self.rowCount()
            self.insertRow(row)
            self.setItem(row, 0, QTableWidgetItem(key))
            self.setItem(row, 1, QTableWidgetItem(value))
            self.setRowHeight(row, self.DEFAULT_ROW_HEIGHT)
        self._adjust_key_column_width()
        self._expanded_row = -1

    def read(self) -> dict:
        """Read the current table contents back into a dict (skips rows with empty key)."""
        params = {}
        for row in range(self.rowCount()):
            key_item = self.item(row, 0)
            if not key_item or not key_item.text().strip():
                continue
            val_item = self.item(row, 1)
            params[key_item.text().strip()] = val_item.text() if val_item else ""
        return params

    def _adjust_key_column_width(self):
        if self.rowCount() == 0:
            self.setColumnWidth(0, self.KEY_COLUMN_FALLBACK)
            return
        fm = self.fontMetrics()
        max_w = 0
        for r in range(self.rowCount()):
            item = self.item(r, 0)
            if item:
                w = fm.horizontalAdvance(item.text())
                max_w = max(max_w, w)
        max_w += self.KEY_COLUMN_PADDING
        viewport_w = self.viewport().width()
        if viewport_w <= 0:
            self.setColumnWidth(0, max_w)
            return
        cap = viewport_w // 2
        self.setColumnWidth(0, min(max_w, cap))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_key_column_width()

    def _expand_row(self, row: int):
        """Set row height large enough to show the full wrapped text of both cells.
        Uses TextWordWrap | TextWrapAnywhere so strings without spaces
        (e.g. URL-encoded values) also wrap and contribute to the height."""
        fm = self.fontMetrics()
        needed = self.DEFAULT_ROW_HEIGHT
        # Word-break first, fall back to character-break for unbreakable strings
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.TextFlag.TextWrapAnywhere)
        v_padding = 12  # top + bottom padding inside cell
        h_padding = 16  # left + right padding inside cell
        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item is None:
                continue
            col_w = max(self.columnWidth(col) - h_padding, 50)
            rect = fm.boundingRect(0, 0, col_w, 10000, flags, item.text() or "")
            h = rect.height() + v_padding
            if h > needed:
                needed = h
        self.setRowHeight(row, needed)

    def mouseDoubleClickEvent(self, event):
        # Expand the row BEFORE the default behavior opens the editor.
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        item = self.itemAt(pos)
        if item is not None:
            row = item.row()
            if self._expanded_row >= 0 and self._expanded_row != row:
                self.setRowHeight(self._expanded_row, self.DEFAULT_ROW_HEIGHT)
            self._expand_row(row)
            self._expanded_row = row
        super().mouseDoubleClickEvent(event)

    def closeEditor(self, editor, hint):
        # Edit actually finished (Enter/Esc/click-outside/Tab) — collapse the row.
        super().closeEditor(editor, hint)
        if self._expanded_row >= 0:
            self.setRowHeight(self._expanded_row, self.DEFAULT_ROW_HEIGHT)
            self._expanded_row = -1

    # NOTE: we intentionally do NOT override focusOutEvent to collapse the row.
    # The table loses focus whenever its own editor takes focus (immediately
    # after double-click), and collapsing there would defeat the expansion.
    # closeEditor above handles the natural "edit finished" collapse.

    def keyPressEvent(self, event):
        # Custom Ctrl+C / Ctrl+X / Ctrl+V on the currently selected cell,
        # without entering edit mode. Operates on the whole cell's text.
        if event.matches(QKeySequence.StandardKey.Copy):
            item = self.currentItem()
            if item is not None:
                QApplication.clipboard().setText(item.text())
                event.accept()
                return
        elif event.matches(QKeySequence.StandardKey.Cut):
            item = self.currentItem()
            if item is not None:
                QApplication.clipboard().setText(item.text())
                item.setText("")
                event.accept()
                return
        elif event.matches(QKeySequence.StandardKey.Paste):
            item = self.currentItem()
            if item is not None:
                item.setText(QApplication.clipboard().text())
                event.accept()
                return
        super().keyPressEvent(event)


# ============================================================
# HTTP worker (background thread)
# ============================================================

class HttpWorker(QObject):
    """Performs HTTP GET in a background thread, emits result via signals.
    Always emits exactly one of `finished` or `failed`, even on unexpected errors.
    Reports elapsed_ms and size_bytes alongside the response for UI display."""

    # code, reason, body, elapsed_ms, size_bytes
    finished = Signal(int, str, str, float, int)
    # error_message, elapsed_ms
    failed = Signal(str, float)

    REQUEST_TIMEOUT_SEC = 10

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(self.url, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.REQUEST_TIMEOUT_SEC) as resp:
                    status = int(resp.status)
                    reason = str(resp.reason or "")
                    raw = resp.read()
                    body = raw.decode("utf-8", errors="replace")
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                self.finished.emit(status, reason, body, elapsed_ms, len(raw))
                return
            except urllib.error.HTTPError as e:
                # 4xx / 5xx — server responded with an error; we still want the body
                try:
                    raw = e.read()
                    body = raw.decode("utf-8", errors="replace")
                except Exception:
                    raw = b""
                    body = ""
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                self.finished.emit(int(e.code), str(e.reason or ""), body, elapsed_ms, len(raw))
                return
            except urllib.error.URLError as e:
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                reason = e.reason
                cls_name = type(reason).__name__
                if cls_name in ("timeout", "TimeoutError"):
                    self.failed.emit(f"Network Error: timed out after {self.REQUEST_TIMEOUT_SEC}s", elapsed_ms)
                else:
                    self.failed.emit(f"Network Error: {reason}", elapsed_ms)
                return
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            self.failed.emit(f"Error: {type(e).__name__}: {e}", elapsed_ms)


# ============================================================
# Main window
# ============================================================

class ConverterWindow(QWidget):

    # Window size targets
    COLLAPSED_W = 600
    COLLAPSED_H = 500
    EXPANDED_W = 1320
    EXPANDED_H = 640
    SCREEN_FILL_RATIO = 0.85
    ANIM_DURATION_MS = 100

    # Status code background palette
    STATUS_COLORS = {
        2: "#c8f0c8",   # 2xx — green
        3: "#c8d8f0",   # 3xx — blue
        4: "#ffe0b0",   # 4xx — orange
        5: "#ffc0c0",   # 5xx — red
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("cURL (bash) → GET request converter v1.2.2")
        self.resize(self.COLLAPSED_W, self.COLLAPSED_H)

        # Guard flag to prevent infinite sync loops
        self._syncing = False

        # HTTP worker references
        self._thread = None
        self._worker = None

        # Geometry animation
        self._anim = None

        # ===== ROOT LAYOUT =====
        root = QHBoxLayout(self)

        # --- LEFT COLUMN ---
        left_widget = QWidget()
        left = QVBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)

        title_row = QHBoxLayout()
        title = QLabel("Paste cURL (bash) type from DevTools")
        title.setFont(QFont("Segoe UI", 14))
        title_row.addWidget(title)

        info_icon = ClickableLabel()
        info_pixmap = QPixmap(resource_path("resources/info.png"))
        info_icon.setPixmap(info_pixmap.scaledToHeight(20, Qt.TransformationMode.SmoothTransformation))
        info_icon.setToolTip("Click it to see instructions")
        info_icon.clicked.connect(self.show_instructions)
        title_row.addWidget(info_icon)
        title_row.addStretch()
        left.addLayout(title_row)

        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("Paste cURL (bash) type here...")
        # Strip any formatting from pasted content (Postman dark theme etc.)
        self.input_box.setAcceptRichText(False)
        left.addWidget(self.input_box)

        convert_btn = QPushButton("Convert to GET request")
        convert_btn.setStyleSheet(
            "QPushButton { background-color: #a3ffb5; padding: 6px; }"
            "QPushButton:hover { background-color: #85e89a; }"
            "QPushButton:pressed { background-color: #63cc7a; }"
        )
        convert_btn.clicked.connect(self.on_convert)
        left.addWidget(convert_btn)

        output_title = QLabel("Converted URL")
        output_title.setFont(QFont("Segoe UI", 14))
        left.addWidget(output_title)

        self.output_box = QTextEdit()
        self.output_box.setPlaceholderText("Converted URL will appear here. You can edit it manually.")
        # Strip any formatting from pasted content (Postman dark theme etc.)
        self.output_box.setAcceptRichText(False)
        self.output_box.textChanged.connect(self._on_converted_url_changed)
        left.addWidget(self.output_box)

        btn_row = QHBoxLayout()

        copy_btn = QPushButton("Copy GET request")
        copy_btn.setStyleSheet(
            "QPushButton { background-color: #f0f2af; padding: 6px; }"
            "QPushButton:hover { background-color: #d8da8e; }"
            "QPushButton:pressed { background-color: #c0c270; }"
        )
        copy_btn.clicked.connect(self.copy_result)
        btn_row.addWidget(copy_btn, 1)

        send_btn = QPushButton("Send GET request")
        send_btn.setStyleSheet(
            "QPushButton { background-color: #b5d4ff; padding: 6px; }"
            "QPushButton:hover { background-color: #9ec0ed; }"
            "QPushButton:pressed { background-color: #7fa6d4; }"
        )
        send_btn.clicked.connect(self.on_send)
        btn_row.addWidget(send_btn, 1)

        left.addLayout(btn_row)

        root.addWidget(left_widget, 1)

        # --- RIGHT PANEL (hidden until first Send) ---
        self.right_panel = QWidget()
        right = QHBoxLayout(self.right_panel)
        right.setContentsMargins(8, 0, 0, 0)

        # Request editor sub-panel
        req_widget = QWidget()
        req = QVBoxLayout(req_widget)
        req.setContentsMargins(0, 0, 4, 0)

        req_label = QLabel("Request")
        req_label.setFont(QFont("Segoe UI", 14))
        req.addWidget(req_label)

        self.request_url_input = ExpandableLineEdit()
        self.request_url_input.setPlaceholderText("Base URL up to '?'  (double-click to expand)")
        self.request_url_input.textChanged.connect(self._on_request_url_changed)
        req.addWidget(self.request_url_input)

        params_label = QLabel("Requests params")
        params_label.setFont(QFont("Segoe UI", 14))
        req.addWidget(params_label)

        self.params_table = ParamsTable()
        self.params_table.itemChanged.connect(self._on_params_changed)
        req.addWidget(self.params_table, 1)

        right.addWidget(req_widget, 1)

        # Response viewer sub-panel
        resp_widget = QWidget()
        resp = QVBoxLayout(resp_widget)
        resp.setContentsMargins(4, 0, 0, 0)

        resp_label = QLabel("Response")
        resp_label.setFont(QFont("Segoe UI", 14))
        resp.addWidget(resp_label)

        # Status row: status_code | time | size. All read-only QLineEdit-s for
        # selection + ПКМ Copy + Ctrl+C. Only status_label gets a colored bg.
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)

        self.status_label = QLineEdit("")
        self.status_label.setReadOnly(True)
        self.status_label.setFrame(False)
        self.status_label.setFont(QFont("Consolas", 10))
        self._apply_status_style(bg="transparent", color="#444")
        status_row.addWidget(self.status_label, 0)

        self.status_time = QLineEdit("")
        self.status_time.setReadOnly(True)
        self.status_time.setFrame(False)
        self.status_time.setFont(QFont("Consolas", 10))
        self.status_time.setStyleSheet(
            "QLineEdit { background: transparent; color: #444; "
            "padding: 2px 6px; border: none; }"
        )
        status_row.addWidget(self.status_time, 0)

        self.status_size = QLineEdit("")
        self.status_size.setReadOnly(True)
        self.status_size.setFrame(False)
        self.status_size.setFont(QFont("Consolas", 10))
        self.status_size.setStyleSheet(
            "QLineEdit { background: transparent; color: #444; "
            "padding: 2px 6px; border: none; }"
        )
        status_row.addWidget(self.status_size, 0)

        status_row.addStretch(1)
        resp.addLayout(status_row)

        self.response_box = JsonResponseEdit()
        resp.addWidget(self.response_box, 1)

        copy_resp_btn = QPushButton("Copy response")
        copy_resp_btn.setStyleSheet(
            "QPushButton { background-color: #f5b8c8; padding: 6px; }"
            "QPushButton:hover { background-color: #e09cae; }"
            "QPushButton:pressed { background-color: #c78294; }"
        )
        copy_resp_btn.clicked.connect(self.copy_response)
        resp.addWidget(copy_resp_btn)

        right.addWidget(resp_widget, 1)

        self.right_panel.setVisible(False)
        root.addWidget(self.right_panel, 2)

    # ============================================================
    # Sync logic
    # ============================================================

    def _on_converted_url_changed(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            url = self.output_box.toPlainText().strip()
            base, params = split_url(url)
            self.request_url_input.setText(base)
            self.params_table.fill(params)
        finally:
            self._syncing = False

    def _on_request_url_changed(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            base = self.request_url_input.text()
            params = self.params_table.read()
            self.output_box.setPlainText(build_url(base, params))
        finally:
            self._syncing = False

    def _on_params_changed(self, _item):
        if self._syncing:
            return
        self._syncing = True
        try:
            base = self.request_url_input.text()
            params = self.params_table.read()
            self.output_box.setPlainText(build_url(base, params))
        finally:
            self._syncing = False

    # ============================================================
    # Actions
    # ============================================================

    def show_instructions(self):
        GifOverlay(resource_path("resources/info.gif"), self)

    def on_convert(self):
        try:
            result = convert_to_url(self.input_box.toPlainText())
            self.output_box.setPlainText(result)
        except Exception as e:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Critical)
            msg.setWindowTitle("Error")
            msg.setText("<center>" + str(e).replace("\n", "<br>") + "</center>")
            msg.exec()
            return

        # If the right panel is currently open, animate it closed and clear the
        # stale response. Next Send on the new URL will reopen it fresh.
        if self.right_panel.isVisible():
            self._set_status_field(self.status_label, "")
            self._apply_status_style(bg="transparent", color="#444")
            self._set_status_field(self.status_time, "")
            self._set_status_field(self.status_size, "")
            self.response_box.setPlainText("")
            self._animate_collapse()

    def copy_result(self):
        QApplication.clipboard().setText(self.output_box.toPlainText())

    def copy_response(self):
        QApplication.clipboard().setText(self.response_box.toPlainText())

    def on_send(self):
        url = self.output_box.toPlainText().strip()
        if not url:
            QMessageBox.warning(self, "Send", "Converted URL is empty.\nPaste cURL and click Convert first.")
            return

        # Open right panel with a smooth animation if it isn't already visible
        if not self.right_panel.isVisible():
            self.right_panel.setVisible(True)
            self._animate_expand()

        # Indicate pending request
        self._set_status_field(self.status_label, "Sending...")
        self._apply_status_style(bg="transparent", color="#444")
        self._set_status_field(self.status_time, "")
        self._set_status_field(self.status_size, "")
        self.response_box.setPlainText("")

        # Detach any in-flight worker so its eventual completion doesn't
        # overwrite the fresh state we're about to set up.
        if self._worker is not None:
            try:
                self._worker.finished.disconnect()
                self._worker.failed.disconnect()
            except (RuntimeError, TypeError):
                pass

        # Fresh thread + worker; cleanup chain through deleteLater (no parent).
        thread = QThread()
        worker = HttpWorker(url)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_response_received)
        worker.failed.connect(self._on_request_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_response_received(self, code: int, reason: str, body: str,
                              elapsed_ms: float, size_bytes: int):
        self._set_status_field(self.status_label, f"Status code: {code} {reason}".rstrip())
        self._set_status_field(self.status_time, self._format_time(elapsed_ms))
        self._set_status_field(self.status_size, self._format_size(size_bytes))

        # Background color by status class (only status_label; time/size stay neutral)
        cls = code // 100
        bg = self.STATUS_COLORS.get(cls, "transparent")
        self._apply_status_style(bg=bg, color="black")

        # Pretty-print JSON if possible
        try:
            parsed = json.loads(body)
            body = json.dumps(parsed, indent=4, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass  # leave as plain text

        self.response_box.setPlainText(body)

    def _on_request_failed(self, error: str, elapsed_ms: float):
        self._set_status_field(self.status_label, error)
        self._set_status_field(self.status_time, self._format_time(elapsed_ms))
        self._set_status_field(self.status_size, "")
        self._apply_status_style(bg="transparent", color="#8b0000")
        self.response_box.setPlainText("")

    def _apply_status_style(self, bg: str, color: str):
        """Apply background + text color to the status field via stylesheet."""
        self.status_label.setStyleSheet(
            f"QLineEdit {{ background: {bg}; color: {color}; "
            f"padding: 2px 6px; border: none; border-radius: 3px; }}"
        )

    # ----- helpers for the status row -----

    def _set_status_field(self, field: QLineEdit, text: str):
        """Set text and shrink the field width to fit (so the 3 fields sit
        compactly side by side, instead of expanding to fill the row)."""
        field.setText(text)
        if text:
            fm = field.fontMetrics()
            w = fm.horizontalAdvance(text) + 18  # padding + slack
            field.setFixedWidth(max(w, 1))
        else:
            field.setFixedWidth(1)

    @staticmethod
    def _format_time(ms: float) -> str:
        if ms >= 1000:
            return f"Time: {ms / 1000:.2f} s"
        return f"Time: {int(round(ms))} ms"

    @staticmethod
    def _format_size(n: int) -> str:
        if n >= 1024 * 1024:
            return f"Size: {n / 1024 / 1024:.2f} MB"
        if n >= 1024:
            return f"Size: {n / 1024:.2f} KB"
        return f"Size: {n} B"

    # ============================================================
    # Window animation
    # ============================================================

    def _target_expanded_geometry(self) -> QRect:
        screen = self.screen() or QApplication.primaryScreen()
        sg = screen.availableGeometry()
        w = min(self.EXPANDED_W, int(sg.width() * self.SCREEN_FILL_RATIO))
        h = min(self.EXPANDED_H, int(sg.height() * self.SCREEN_FILL_RATIO))
        x = sg.x() + (sg.width() - w) // 2
        y = sg.y() + (sg.height() - h) // 2
        return QRect(x, y, w, h)

    def _target_collapsed_geometry(self) -> QRect:
        screen = self.screen() or QApplication.primaryScreen()
        sg = screen.availableGeometry()
        w = self.COLLAPSED_W
        h = self.COLLAPSED_H
        x = sg.x() + (sg.width() - w) // 2
        y = sg.y() + (sg.height() - h) // 2
        return QRect(x, y, w, h)

    def _animate_geometry(self, target: QRect, on_finished=None):
        """Animate the window geometry to `target` over ANIM_DURATION_MS.
        Cancels any in-flight animation first."""
        if self._anim is not None:
            try:
                self._anim.stop()
                self._anim.deleteLater()
            except RuntimeError:
                pass
            self._anim = None

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(self.ANIM_DURATION_MS)
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(target)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)
        if on_finished is not None:
            self._anim.finished.connect(on_finished)
        self._anim.start()

    def _animate_expand(self):
        self._animate_geometry(self._target_expanded_geometry())

    def _animate_collapse(self):
        # Hide right_panel only after the window has finished shrinking,
        # otherwise the layout would briefly squish the left column.
        def _hide_panel():
            self.right_panel.setVisible(False)
        self._animate_geometry(self._target_collapsed_geometry(), on_finished=_hide_panel)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path("resources/icon.ico")))
    window = ConverterWindow()
    window.show()
    sys.exit(app.exec())
