import sys
import os
import re
import json
import time
import uuid
import urllib.parse
import urllib.request
import urllib.error

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QMessageBox,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QStyledItemDelegate, QPlainTextEdit, QTabWidget, QTabBar, QInputDialog, QMenu,
)
from PySide6.QtGui import (
    QFont, QIcon, QPixmap, QMovie, QPainter, QColor, QKeySequence,
    QTextOption, QTextCursor, QPen,
)
from PySide6.QtCore import (
    Qt, Signal, QObject, QThread, QTimer,
    QRect, QSize, QPropertyAnimation, QEasingCurve,
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


def tab_title_from_url(url: str) -> str:
    """Derive a short tab title from a URL — its host name, or a fallback."""
    url = (url or "").strip()
    if not url:
        return "New tab"
    try:
        netloc = urllib.parse.urlparse(url).netloc
    except Exception:
        netloc = ""
    if netloc:
        # drop any credentials and the port
        netloc = netloc.split("@")[-1].split(":")[0]
        if netloc:
            return netloc
    return "New tab"


def url_without_params(url: str) -> str:
    """Return the URL up to and including the question mark.
    If there is no query, the URL is returned unchanged."""
    url = (url or "").strip()
    q = url.find("?")
    if q >= 0:
        return url[:q + 1]
    return url


# ============================================================
# Session persistence (v1.5.0) — remember tabs between launches
# ============================================================

def _session_file_path() -> str:
    """Return the path to the session.json file in the user app-data dir."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "URL_Converter", "session.json")


# ============================================================
# cURL request detection (v1.3.0)
# ============================================================

class ParsedRequest:
    """Result of analysing a copied cURL (bash) command.

    method        -- "GET" or "POST"
    content_type  -- clean MIME type for display (no boundary), or "" if absent
    body_format   -- "url-encoded" | "multipart" | "json" | "binary" | "none"
    base_url      -- the URL taken from the curl command (may include a query)
    params        -- dict of key/value pairs (only for url-encoded / multipart)
    body_text     -- raw request body (only for json / binary)
    """

    def __init__(self, method="GET", content_type="", body_format="none",
                 base_url="", params=None, body_text="", headers=None):
        self.method = method
        self.content_type = content_type
        self.body_format = body_format
        self.base_url = base_url
        self.params = params or {}
        self.body_text = body_text
        self.headers = headers or {}


# curl body options, ordered so the more specific ones are tried first
_BODY_OPTS = ("--data-raw", "--data-binary", "--data-ascii",
              "--data-urlencode", "--data", "-d")


def extract_body(text: str):
    """Pull the request body out of a cURL command.
    Handles both ANSI-C quoted ($'...') and plain ('...') forms."""
    for opt in _BODY_OPTS:
        esc = re.escape(opt)
        m = re.search(esc + r" \$'(.*?)'", text, re.DOTALL)
        if m:
            return m.group(1)
        m = re.search(esc + r" '(.*?)'", text, re.DOTALL)
        if m:
            return m.group(1)
    return None


def extract_content_type(text: str) -> str:
    """Read the Content-Type header from a cURL command.
    Returns a clean MIME type (parameters such as boundary/charset stripped)."""
    m = re.search(r"-H '[Cc]ontent-[Tt]ype:\s*([^']+)'", text)
    if not m:
        return ""
    return m.group(1).split(";")[0].strip()


# headers that must NOT be forwarded as-is — we set them ourselves, urllib
# manages them, or forwarding them would break the response (compressed body)
_SKIP_HEADERS = {"content-type", "content-length", "host",
                 "accept-encoding", "connection"}


def extract_headers(text: str) -> dict:
    """Collect every -H 'Name: Value' header from a cURL command.
    Headers in _SKIP_HEADERS are dropped (set by us / managed by urllib)."""
    headers = {}
    for m in re.finditer(r"-H '([^']*)'", text):
        raw = m.group(1)
        if ":" not in raw:
            continue
        name, value = raw.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name and name.lower() not in _SKIP_HEADERS:
            headers[name] = value
    return headers


def detect_request(text: str) -> ParsedRequest:
    """Analyse a copied cURL (bash) command and return a ParsedRequest."""
    text = text.strip()

    url_match = re.search(r"curl '([^']+)'", text)
    if not url_match:
        raise ValueError(
            "It's not cURL (bash) type URL.\n\n"
            "Paste cURL (bash) type URL from DevTools please."
        )
    base_url = url_match.group(1)

    content_type = extract_content_type(text)
    headers = extract_headers(text)
    body = extract_body(text)

    # --- method ---
    method = "POST" if body is not None else "GET"
    if re.search(r"(-X|--request)\s+'?POST'?", text):
        method = "POST"
    if body is None and re.search(r"(-X|--request)\s+'?GET'?", text):
        method = "GET"

    # --- body format ---
    ct_low = content_type.lower()
    if body is None:
        body_format = "none"
    elif "json" in ct_low:
        body_format = "json"
    elif "multipart" in ct_low:
        body_format = "multipart"
    elif "x-www-form-urlencoded" in ct_low:
        body_format = "url-encoded"
    else:
        # No (or unhelpful) Content-Type: guess from the body shape
        stripped = body.lstrip()
        if stripped[:1] in ("{", "["):
            body_format = "json"
        elif 'name="' in body and ("\\r\\n" in body or "------" in body):
            body_format = "multipart"
        elif re.match(r"[^=&\s]+=", body):
            body_format = "url-encoded"
        else:
            body_format = "binary"

    # --- params / body text ---
    params = {}
    body_text = ""
    if body_format == "url-encoded":
        params = parse_url_encoded(body)
    elif body_format == "multipart":
        params = parse_multipart(body)
        if not params:
            # multipart that we couldn't parse — keep the raw body instead
            body_format = "binary"
            body_text = body
    elif body_format in ("json", "binary"):
        body_text = body

    return ParsedRequest(
        method=method,
        content_type=content_type,
        body_format=body_format,
        base_url=base_url,
        params=params,
        body_text=body_text,
        headers=headers,
    )


def build_converted_url(pr: ParsedRequest) -> str:
    """Build the 'Converted URL' for a parsed request.
    For form-style bodies the params are appended as a query string (same as
    v1.2.x), so the URL/params triangle keeps working. For json/binary the
    body is not part of the URL, so just the base URL is returned."""
    if pr.body_format in ("json", "binary") or not pr.params:
        return pr.base_url
    separator = "&" if "?" in pr.base_url else "?"
    return f"{pr.base_url}{separator}{urllib.parse.urlencode(pr.params)}"


def build_multipart_body(params: dict):
    """Reconstruct a multipart/form-data body from a dict.
    Returns (body_bytes, content_type_with_boundary)."""
    boundary = "----CurlConverterBoundary" + uuid.uuid4().hex
    lines = []
    for key, value in params.items():
        lines.append("--" + boundary)
        lines.append(f'Content-Disposition: form-data; name="{key}"')
        lines.append("")
        lines.append(value)
    lines.append("--" + boundary + "--")
    lines.append("")
    body = "\r\n".join(lines).encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def pretty_json(text: str) -> str:
    """Pretty-print a JSON string; return the original text if it isn't JSON."""
    try:
        return json.dumps(json.loads(text), indent=4, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        return text


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
        # scroll back to the start of the URL so its beginning is visible,
        # not wherever the caret happened to be while editing
        self.moveCursor(QTextCursor.MoveOperation.Start)
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


class JsonBodyEdit(QPlainTextEdit):
    """Editable monospace text editor for a JSON / raw request body.
    Used instead of the key/value table when the request body is not a
    set of form fields. Long lines are NOT wrapped (horizontal scroll),
    keeping pretty-printed JSON readable."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Consolas", 10))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setPlaceholderText("Request body (JSON / raw) — editable.")


# ============================================================
# HTTP worker (background thread)
# ============================================================

class HttpWorker(QObject):
    """Performs an HTTP request (GET or POST) in a background thread and emits
    the result via signals. Always emits exactly one of `finished` or `failed`,
    even on unexpected errors. Reports elapsed_ms and size_bytes for UI display."""

    # code, reason, body, elapsed_ms, size_bytes
    finished = Signal(int, str, str, float, int)
    # error_message, elapsed_ms
    failed = Signal(str, float)

    REQUEST_TIMEOUT_SEC = 10

    def __init__(self, url: str, method: str = "GET",
                 data: bytes = None, content_type: str = None, headers: dict = None):
        super().__init__()
        self.url = url
        self.method = method
        self.data = data
        self.content_type = content_type
        self.headers = headers or {}

    def run(self):
        t0 = time.monotonic()
        try:
            headers = dict(self.headers)
            if self.content_type:
                headers["Content-Type"] = self.content_type
            req = urllib.request.Request(
                self.url, data=self.data, method=self.method, headers=headers
            )
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
# Converter page — one independent request (one tab)
# ============================================================

class ConverterPage(QWidget):
    """A single, fully independent cURL -> request converter.
    One ConverterPage lives in each tab and owns its own state, HTTP worker
    and response. It emits:
      - requestSent : when Send is pressed (the window expands on the first one)
      - urlChanged  : when the converted URL changes (title + tooltip)
    """

    requestSent = Signal()
    urlChanged = Signal(str)

    # Status code background palette
    STATUS_COLORS = {
        2: "#c8f0c8",   # 2xx — green
        3: "#c8d8f0",   # 3xx — blue
        4: "#ffe0b0",   # 4xx — orange
        5: "#ffc0c0",   # 5xx — red
    }

    # Request type indicator colors
    METHOD_GET_BG = "#C8F0C8"        # GET — green
    METHOD_POST_BG = "#D8DA8E"       # POST — yellow
    CTYPE_JSON_BG = "#B5D4FF"        # JSON content-type — blue
    CTYPE_GET_OTHER_BG = "#DEF6DE"   # other content-type on GET — light green
    CTYPE_POST_OTHER_BG = "#E8E9BB"  # other content-type on POST — light yellow

    def __init__(self):
        super().__init__()

        # Guard flag to prevent infinite sync loops
        self._syncing = False

        # Last detected request (set by Convert); drives Send + the indicator
        self._parsed = None
        # True when the body is shown as a JSON/raw text editor instead of a table
        self._json_mode = False

        # HTTP worker references
        self._thread = None
        self._worker = None

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

        convert_btn = QPushButton("Convert from cURL (bash) type to request")
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

        copy_btn = QPushButton("Copy request")
        copy_btn.setStyleSheet(
            "QPushButton { background-color: #f0f2af; padding: 6px; }"
            "QPushButton:hover { background-color: #d8da8e; }"
            "QPushButton:pressed { background-color: #c0c270; }"
        )
        copy_btn.clicked.connect(self.copy_result)
        btn_row.addWidget(copy_btn, 1)

        send_btn = QPushButton("Send request")
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

        # Request type indicator: method + (optional) content-type.
        # Both are selectable + copyable (Ctrl+C / right-click Copy).
        indicator_row = QHBoxLayout()
        indicator_row.setContentsMargins(0, 0, 0, 2)
        indicator_row.setSpacing(6)

        self.method_label = QLabel("")
        self.method_label.setFont(QFont("Consolas", 10))
        self.method_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.method_label.setVisible(False)
        indicator_row.addWidget(self.method_label, 0)

        self.ctype_label = QLabel("")
        self.ctype_label.setFont(QFont("Consolas", 10))
        self.ctype_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.ctype_label.setVisible(False)
        indicator_row.addWidget(self.ctype_label, 0)

        indicator_row.addStretch(1)
        req.addLayout(indicator_row)

        self.request_url_input = ExpandableLineEdit()
        self.request_url_input.setPlaceholderText("Base URL up to '?'  (double-click to expand)")
        self.request_url_input.textChanged.connect(self._on_request_url_changed)
        req.addWidget(self.request_url_input)

        params_label = QLabel("Requests params")
        params_label.setFont(QFont("Segoe UI", 14))
        req.addWidget(params_label)

        # Form-style body — key/value table (default)
        self.params_table = ParamsTable()
        self.params_table.itemChanged.connect(self._on_params_changed)
        req.addWidget(self.params_table, 1)

        # JSON / raw body — text editor (shown instead of the table when needed)
        self.json_editor = JsonBodyEdit()
        self.json_editor.setVisible(False)
        req.addWidget(self.json_editor, 1)

        right.addWidget(req_widget, 1)

        # Response viewer sub-panel
        resp_widget = QWidget()
        resp = QVBoxLayout(resp_widget)
        resp.setContentsMargins(4, 0, 0, 0)

        resp_label = QLabel("Response")
        resp_label.setFont(QFont("Segoe UI", 14))
        resp.addWidget(resp_label)

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
            # let the window refresh this tab's title + tooltip
            self.urlChanged.emit(url)
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
    # Request type indicator
    # ============================================================

    def _set_json_mode(self, on: bool):
        """Toggle the request-body editor between the key/value table (form
        bodies) and the JSON/raw text editor."""
        self._json_mode = on
        self.params_table.setVisible(not on)
        self.json_editor.setVisible(on)

    def _update_indicator(self):
        """Refresh the method / content-type indicator from self._parsed."""
        pr = self._parsed
        if pr is None:
            self.method_label.setVisible(False)
            self.ctype_label.setVisible(False)
            return

        if pr.method == "POST":
            m_bg = self.METHOD_POST_BG
        else:
            m_bg = self.METHOD_GET_BG
        self.method_label.setText(pr.method)
        self.method_label.setStyleSheet(
            f"QLabel {{ background: {m_bg}; padding: 2px 8px; border-radius: 3px; }}"
        )
        self.method_label.setVisible(True)

        if pr.content_type:
            if "json" in pr.content_type.lower():
                c_bg = self.CTYPE_JSON_BG
            elif pr.method == "POST":
                c_bg = self.CTYPE_POST_OTHER_BG
            else:
                c_bg = self.CTYPE_GET_OTHER_BG
            self.ctype_label.setText(pr.content_type)
            self.ctype_label.setStyleSheet(
                f"QLabel {{ background: {c_bg}; padding: 2px 8px; border-radius: 3px; }}"
            )
            self.ctype_label.setVisible(True)
        else:
            self.ctype_label.setVisible(False)

    # ============================================================
    # Actions
    # ============================================================

    def copy_state_from(self, other):
        """Copy another page's request into this one (used by Duplicate Tab).
        The raw cURL (bash) input is intentionally NOT copied — only the
        converted request: URL, params, detected method and JSON body."""
        self._parsed = other._parsed
        self._set_json_mode(other._json_mode)
        self.json_editor.setPlainText(other.json_editor.toPlainText())
        # setting the Converted URL triggers the sync (Request URL + params)
        self.output_box.setPlainText(other.output_box.toPlainText())
        self._update_indicator()

    def show_instructions(self):
        GifOverlay(resource_path("resources/info.gif"), self.window())

    def on_convert(self):
        try:
            pr = detect_request(self.input_box.toPlainText())
        except Exception as e:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Critical)
            msg.setWindowTitle("Error")
            msg.setText("<center>" + str(e).replace("\n", "<br>") + "</center>")
            msg.exec()
            return

        self._parsed = pr

        if pr.body_format in ("json", "binary"):
            self._set_json_mode(True)
            self.json_editor.setPlainText(pretty_json(pr.body_text))
            self.output_box.setPlainText(build_converted_url(pr))
        else:
            self._set_json_mode(False)
            self.json_editor.setPlainText("")
            self.output_box.setPlainText(build_converted_url(pr))

        self._update_indicator()

        if self.right_panel.isVisible():
            self._set_status_field(self.status_label, "")
            self._apply_status_style(bg="transparent", color="#444")
            self._set_status_field(self.status_time, "")
            self._set_status_field(self.status_size, "")
            self.response_box.setPlainText("")
            self.right_panel.setVisible(False)

    def copy_result(self):
        QApplication.clipboard().setText(self.output_box.toPlainText())

    def copy_response(self):
        QApplication.clipboard().setText(self.response_box.toPlainText())

    def on_send(self):
        pr = self._parsed
        method = "GET"
        data = None
        content_type = None
        headers = pr.headers if pr is not None else {}

        if pr is not None and pr.method == "POST":
            method = "POST"
            base = self.request_url_input.text().strip().rstrip("?")
            if self._json_mode:
                url = self.output_box.toPlainText().strip()
                data = self.json_editor.toPlainText().encode("utf-8")
                content_type = pr.content_type or "application/json"
            elif pr.body_format == "multipart":
                url = base
                data, content_type = build_multipart_body(self.params_table.read())
            else:
                url = base
                data = urllib.parse.urlencode(self.params_table.read()).encode("utf-8")
                content_type = pr.content_type or "application/x-www-form-urlencoded"
        else:
            url = self.output_box.toPlainText().strip()

        if not url:
            QMessageBox.warning(self, "Send", "Converted URL is empty.\nPaste cURL and click Convert first.")
            return

        if not self.right_panel.isVisible():
            self.right_panel.setVisible(True)
        self.requestSent.emit()

        self._set_status_field(self.status_label, "Sending...")
        self._apply_status_style(bg="transparent", color="#444")
        self._set_status_field(self.status_time, "")
        self._set_status_field(self.status_size, "")
        self.response_box.setPlainText("")

        if self._worker is not None:
            try:
                self._worker.finished.disconnect()
                self._worker.failed.disconnect()
            except (RuntimeError, TypeError):
                pass

        thread = QThread()
        worker = HttpWorker(url, method, data, content_type, headers)
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

        cls = code // 100
        bg = self.STATUS_COLORS.get(cls, "transparent")
        self._apply_status_style(bg=bg, color="black")

        try:
            parsed = json.loads(body)
            body = json.dumps(parsed, indent=4, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass

        # JSON / plain text — no wrap (horizontal scroll), unlike error text
        self.response_box.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.response_box.setPlainText(body)

    def _on_request_failed(self, error: str, elapsed_ms: float):
        # Keep the status field short so a long error cannot widen the
        # panel; the full error text goes into the word-wrapped response box.
        self._set_status_field(self.status_label, "Error")
        self._set_status_field(self.status_time, self._format_time(elapsed_ms))
        self._set_status_field(self.status_size, "")
        self._apply_status_style(bg="transparent", color="#8b0000")
        self.response_box.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.response_box.setPlainText(error)

    def _apply_status_style(self, bg: str, color: str):
        self.status_label.setStyleSheet(
            f"QLineEdit {{ background: {bg}; color: {color}; "
            f"padding: 2px 6px; border: none; border-radius: 3px; }}"
        )

    def _set_status_field(self, field: QLineEdit, text: str):
        field.setText(text)
        if text:
            fm = field.fontMetrics()
            w = fm.horizontalAdvance(text) + 18
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
    # Session persistence (v1.5.0)
    # ============================================================

    def to_dict(self) -> dict:
        """Serialise this page request state to a JSON-safe dict.
        cURL input, response and status fields are intentionally NOT saved."""
        pr = self._parsed
        return {
            "output_text": self.output_box.toPlainText(),
            "json_text": self.json_editor.toPlainText(),
            "json_mode": self._json_mode,
            "parsed": None if pr is None else {
                "method": pr.method,
                "content_type": pr.content_type,
                "body_format": pr.body_format,
                "base_url": pr.base_url,
                "params": pr.params,
                "body_text": pr.body_text,
                "headers": pr.headers,
            },
        }

    def from_dict(self, data: dict) -> None:
        """Restore this page from a dict produced by to_dict()."""
        pd = data.get("parsed")
        if pd:
            self._parsed = ParsedRequest(
                method=pd.get("method", "GET"),
                content_type=pd.get("content_type", ""),
                body_format=pd.get("body_format", "none"),
                base_url=pd.get("base_url", ""),
                params=pd.get("params", {}),
                body_text=pd.get("body_text", ""),
                headers=pd.get("headers", {}),
            )
        else:
            self._parsed = None
        self._set_json_mode(bool(data.get("json_mode", False)))
        self.json_editor.setPlainText(data.get("json_text", ""))
        # setting the Converted URL triggers the sync (Request URL + params)
        self.output_box.setPlainText(data.get("output_text", ""))
        self._update_indicator()
        # Show the right panel right away so the restored request is visible
        # without having to click Send first.
        if self._parsed is not None or self.output_box.toPlainText().strip():
            self.right_panel.setVisible(True)


# ============================================================
# Tab bar — two-line tabs with an inline description editor
# ============================================================

class PlusButton(QPushButton):
    """Small square button that paints a perfectly centred '+' glyph itself,
    so the symbol is always dead-centre regardless of font metrics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("")

    def paintEvent(self, event):
        super().paintEvent(event)  # button background / border / hover
        painter = QPainter(self)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        arm = 5
        pen = QPen(QColor("#333333"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(round(cx - arm), round(cy), round(cx + arm), round(cy))
        painter.drawLine(round(cx), round(cy - arm), round(cx), round(cy + arm))
        painter.end()


class RequestTabBar(QTabBar):
    """Custom tab bar with a single FIXED height (never changes):
      - tabText (set by the window) is the request host;
      - an optional description (stored in tabData) is shown as a compact
        line above the host — both fit inside the fixed height;
      - double-click a tab to edit its description inline;
      - a '+' button sits right after the last tab;
      - the active tab is painted lighter (#F5F5F5) than the inactive ones.
    """

    newTabRequested = Signal()
    duplicateTabRequested = Signal(int)
    closeTabRequested = Signal(int)

    TAB_H = 46            # fixed tab / tab-bar height — never changes
    MIN_TAB_W = 90        # tabs shrink down to this when many are open
    MAX_TAB_W = 240       # preferred tab width when there is room
    PLUS_SPACE = 42       # width kept free at the right for the "+" button
    BG_BAR = "#ECECEC"
    BG_ACTIVE = "#F5F5F5"
    BG_INACTIVE = "#DEDEDE"
    BORDER = "#BFBFBF"

    def __init__(self, parent=None):
        super().__init__(parent)

        # initialise these first — Qt may call tabLayoutChange() during the
        # setExpanding / setDrawBase calls below, before the button exists
        self._plus = None
        self._editor = None
        self._editing_index = -1
        self._dragging = False
        self._press_on_tab = False
        self._grab_dx = 0
        self._drag_mouse_x = 0

        self.setExpanding(False)
        self.setDrawBase(False)

        # "+" new-tab button — a child of the bar, kept just after the last tab
        self._plus = PlusButton(self)
        self._plus.setCursor(Qt.CursorShape.PointingHandCursor)
        self._plus.setFixedSize(28, 24)
        self._plus.setToolTip("New tab")
        self._plus.setStyleSheet(
            "QPushButton { border: 1px solid #bfbfbf; border-radius: 4px;"
            " background: #e6e6e6; }"
            "QPushButton:hover { background: #d4d4d4; }"
        )
        self._plus.clicked.connect(lambda: self.newTabRequested.emit())

    # ----- sizing — ONE fixed height, always -----

    def tabSizeHint(self, index: int) -> QSize:
        # Browser-style: tabs share the available width and shrink as more
        # are opened (down to MIN_TAB_W), so the '+' button stays reachable.
        n = max(self.count(), 1)
        avail = max(self.width() - self.PLUS_SPACE, self.MIN_TAB_W)
        w = max(self.MIN_TAB_W, min(self.MAX_TAB_W, avail // n))
        return QSize(w, self.TAB_H)

    def minimumTabSizeHint(self, index: int) -> QSize:
        return QSize(90, self.TAB_H)

    def sizeHint(self) -> QSize:
        # pin the bar height so it can never grow / multiply on relayout
        return QSize(super().sizeHint().width(), self.TAB_H)

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), self.TAB_H)

    # ----- painting -----

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self.BG_BAR))

        # the tab being dragged is painted last so it floats above the rest
        dragged = self.currentIndex() if self._dragging else -1
        for i in range(self.count()):
            if i == dragged:
                continue
            rect = self.tabRect(i)
            if rect.isValid():
                self._draw_tab(painter, i, rect)

        if dragged >= 0:
            base = self.tabRect(dragged)
            if base.isValid():
                x = self._drag_mouse_x - self._grab_dx
                x = max(0, min(x, self.width() - base.width()))
                self._draw_tab(painter, dragged,
                               QRect(x, base.y(), base.width(), base.height()))
        painter.end()

    def _draw_tab(self, painter, i, rect):
        fm = self.fontMetrics()
        selected = (i == self.currentIndex())
        painter.fillRect(rect, QColor(self.BG_ACTIVE if selected else self.BG_INACTIVE))
        painter.setPen(QColor(self.BORDER))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

        auto = self.tabText(i) or "New tab"
        desc = self.tabData(i) or ""
        # leave room on the left, and on the right for the close button
        text_rect = rect.adjusted(10, 2, -26, -2)
        two_line = bool(desc) or (i == self._editing_index)

        if two_line:
            half = text_rect.height() // 2
            top = QRect(text_rect.x(), text_rect.y(), text_rect.width(), half)
            bot = QRect(text_rect.x(), text_rect.y() + half,
                        text_rect.width(), text_rect.height() - half)
            # description (top) — hidden while it is being edited
            if i != self._editing_index:
                painter.setPen(QColor("#1f1f1f"))
                painter.drawText(
                    top, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    fm.elidedText(desc, Qt.TextElideMode.ElideRight, top.width()),
                )
            # request host (bottom) — slightly muted
            painter.setPen(QColor("#5a5a5a"))
            painter.drawText(
                bot, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                fm.elidedText(auto, Qt.TextElideMode.ElideRight, bot.width()),
            )
        else:
            painter.setPen(QColor("#1f1f1f"))
            painter.drawText(
                text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                fm.elidedText(auto, Qt.TextElideMode.ElideRight, text_rect.width()),
            )

    # ----- drag — let the dragged tab follow the cursor -----

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._dragging = False
        self._press_on_tab = False
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            idx = self.tabAt(pos)
            if idx >= 0:
                self._press_on_tab = True
                self._grab_dx = pos.x() - self.tabRect(idx).x()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self._press_on_tab and (event.buttons() & Qt.MouseButton.LeftButton):
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            self._drag_mouse_x = pos.x()
            self._dragging = True
            self.update()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._dragging:
            self._dragging = False
            self.update()

    # ----- '+' button placement -----

    def tabLayoutChange(self):
        super().tabLayoutChange()
        self._reposition_plus()

    def tabInserted(self, index):
        super().tabInserted(index)
        self._reposition_plus()

    def tabRemoved(self, index):
        super().tabRemoved(index)
        if self._editor is not None:
            self._commit_edit()
        self._reposition_plus()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_plus()

    def showEvent(self, event):
        super().showEvent(event)
        self._reposition_plus()

    def _reposition_plus(self):
        if self._plus is None:
            return
        if self.count() > 0:
            last = self.tabRect(self.count() - 1)
            x = last.right() + 6
        else:
            x = 6
        # vertically centre the button in the tab bar
        y = (self.height() - self._plus.height()) // 2
        # never let the button slide off the right edge of the bar
        x = max(4, min(x, self.width() - self._plus.width() - 4))
        self._plus.move(x, max(0, y))
        self._plus.raise_()

    # ----- right-click context menu -----

    def contextMenuEvent(self, event):
        index = self.tabAt(event.pos())
        if index < 0:
            return
        menu = QMenu(self)
        act_dup = menu.addAction("Duplicate Tab")
        act_close = menu.addAction("Close Tab")
        chosen = menu.exec(event.globalPos())
        if chosen is act_dup:
            self.duplicateTabRequested.emit(index)
        elif chosen is act_close:
            self.closeTabRequested.emit(index)

    # ----- inline description editing -----

    def mouseDoubleClickEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.tabAt(pos)
        if index < 0:
            super().mouseDoubleClickEvent(event)
            return
        self._begin_edit(index)

    def _begin_edit(self, index: int):
        self._commit_edit()  # finish any previous edit first
        self.setCurrentIndex(index)
        self._editing_index = index

        editor = QLineEdit(self)
        editor.setText(self.tabData(index) or "")
        editor.setPlaceholderText("description")
        editor.setStyleSheet(
            "QLineEdit { border: 1px solid #888; border-radius: 3px;"
            " background: #ffffff; padding: 0px 4px; }"
        )
        editor.editingFinished.connect(self._commit_edit)
        self._editor = editor

        # the tab height is fixed, so the editor can be placed straight away
        rect = self.tabRect(index)
        half = rect.height() // 2
        m = 3
        editor.setGeometry(
            rect.x() + m, rect.y() + m,
            max(rect.width() - 2 * m - 22, 40), max(half - m + 2, 16),
        )
        editor.show()
        editor.setFocus()
        editor.selectAll()
        self.update()

    def _commit_edit(self):
        if self._editor is None:
            return
        editor = self._editor
        index = self._editing_index
        self._editor = None
        self._editing_index = -1
        desc = editor.text().strip()
        editor.deleteLater()
        if 0 <= index < self.count():
            self.setTabData(index, desc)
        self.update()
        self._reposition_plus()


# ============================================================
# Main window — a tab strip of independent converter pages
# ============================================================

class ConverterWindow(QWidget):

    # Window size targets
    COLLAPSED_W = 600
    COLLAPSED_H = 540
    EXPANDED_W = 1320
    EXPANDED_H = 700
    SCREEN_FILL_RATIO = 0.85
    ANIM_DURATION_MS = 100

    def __init__(self):
        super().__init__()
        self.setWindowTitle("cURL (bash) → request converter v1.5.0")
        self.resize(self.COLLAPSED_W, self.COLLAPSED_H)

        # Geometry animation — the window expands once, on the first Send
        self._anim = None
        self._expanded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.tabs = QTabWidget()
        self._tabbar = RequestTabBar()
        self.tabs.setTabBar(self._tabbar)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self._tabbar.newTabRequested.connect(self._add_tab)
        self._tabbar.duplicateTabRequested.connect(self._duplicate_tab)
        self._tabbar.closeTabRequested.connect(self._close_tab)
        layout.addWidget(self.tabs)

        # Start with one empty tab — or restore the previous session.
        if not self._try_load_session():
            self._add_tab()

    # ============================================================
    # Tab management
    # ============================================================

    def _add_tab(self):
        page = ConverterPage()
        page.requestSent.connect(self._expand)
        page.urlChanged.connect(lambda url, p=page: self._on_page_url(p, url))
        index = self.tabs.addTab(page, "New tab")
        self.tabs.setCurrentIndex(index)
        return page

    def _close_tab(self, index):
        page = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if page is not None:
            page.deleteLater()
        # Always keep at least one tab open
        if self.tabs.count() == 0:
            self._add_tab()

    def _duplicate_tab(self, index):
        src = self.tabs.widget(index)
        if src is None:
            return
        page = ConverterPage()
        page.requestSent.connect(self._expand)
        page.urlChanged.connect(lambda url, p=page: self._on_page_url(p, url))
        new_index = index + 1
        self.tabs.insertTab(new_index, page, "New tab")
        page.copy_state_from(src)
        # carry the description (top line) over to the duplicate
        desc = self._tabbar.tabData(index)
        if desc:
            self._tabbar.setTabData(new_index, desc)
        self.tabs.setCurrentIndex(new_index)

    def _on_page_url(self, page, url):
        # Refresh this tab's auto-title (host) and its hover tooltip (url to '?').
        index = self.tabs.indexOf(page)
        if index < 0:
            return
        self.tabs.setTabText(index, tab_title_from_url(url))
        self.tabs.setTabToolTip(index, url_without_params(url))
        self._tabbar.updateGeometry()
        self._tabbar.update()

    # ============================================================
    # Window expand animation (one-way: expands on the first Send)
    # ============================================================

    def _expand(self):
        if self._expanded:
            return
        self._expanded = True
        self._animate_geometry(self._target_expanded_geometry())

    def _target_expanded_geometry(self) -> QRect:
        screen = self.screen() or QApplication.primaryScreen()
        sg = screen.availableGeometry()
        w = min(self.EXPANDED_W, int(sg.width() * self.SCREEN_FILL_RATIO))
        h = min(self.EXPANDED_H, int(sg.height() * self.SCREEN_FILL_RATIO))
        x = sg.x() + (sg.width() - w) // 2
        y = sg.y() + (sg.height() - h) // 2
        return QRect(x, y, w, h)

    def _animate_geometry(self, target: QRect):
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
        self._anim.start()

    # ============================================================
    # Session persistence (v1.5.0)
    # ============================================================

    def closeEvent(self, event):
        self._save_session()
        super().closeEvent(event)

    def _save_session(self) -> None:
        tabs_data = []
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            if page is None:
                continue
            d = page.to_dict()
            desc = self._tabbar.tabData(i)
            if desc:
                d["description"] = desc
            tabs_data.append(d)
        data = {
            "version": 1,
            "tabs": tabs_data,
            "active_index": self.tabs.currentIndex(),
        }
        path = _session_file_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[session] save failed: {e}", file=sys.stderr)

    def _try_load_session(self) -> bool:
        """Restore tabs from the saved session. Returns True on success."""
        path = _session_file_path()
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != 1:
                return False
            tabs_data = data.get("tabs", [])
            if not tabs_data:
                return False
            any_request = False
            for td in tabs_data:
                # Skip completely-empty tabs.
                if not td.get("parsed") and not (td.get("output_text") or "").strip():
                    continue
                page = ConverterPage()
                page.requestSent.connect(self._expand)
                page.urlChanged.connect(lambda url, p=page: self._on_page_url(p, url))
                self.tabs.addTab(page, "New tab")
                page.from_dict(td)
                desc = td.get("description") or ""
                if desc:
                    idx = self.tabs.indexOf(page)
                    self._tabbar.setTabData(idx, desc)
                if page._parsed is not None:
                    any_request = True
            if self.tabs.count() == 0:
                return False
            active = max(0, min(int(data.get("active_index", 0)), self.tabs.count() - 1))
            self.tabs.setCurrentIndex(active)
            # If any tab had a parsed request, expand the window now.
            if any_request:
                self._expanded = True
                self.setGeometry(self._target_expanded_geometry())
            return True
        except Exception as e:
            print(f"[session] load failed: {e}", file=sys.stderr)
            return False


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path("resources/icon.ico")))
    window = ConverterWindow()
    window.show()
    sys.exit(app.exec())
