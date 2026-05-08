import sys
import os
import re
import json
import urllib.parse
from typing import Optional, Tuple

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QMessageBox,
)
from PySide6.QtGui import QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtCore import QTimer


# Needed for PyInstaller --onefile
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# ── Format detection ───────────────────────────────────────────────────────────

FORMAT_LABELS = {
    'curl_bash': 'cURL (bash)',
    'curl_cmd':  'cURL (cmd)',
    'powershell': 'PowerShell (Invoke-WebRequest / Invoke-RestMethod)',
    'fetch': 'fetch (JavaScript)',
}

def detect_format(text: str) -> str:
    stripped = text.strip()
    lower = stripped.lower()

    if lower.startswith('curl'):
        # CMD uses ^ for line continuation, bash uses backslash
        if re.search(r'\^\s*\n', stripped):
            return 'curl_cmd'
        return 'curl_bash'

    if 'invoke-webrequest' in lower or 'invoke-restmethod' in lower:
        return 'powershell'

    if re.match(r'\s*fetch\s*\(', stripped, re.IGNORECASE):
        return 'fetch'

    raise ValueError(
        "Unrecognized format.\n\n"
        "Supported inputs:\n"
        "  • cURL (bash)\n"
        "  • cURL (cmd)\n"
        "  • PowerShell (Invoke-WebRequest / Invoke-RestMethod)\n"
        "  • fetch (browser or Node.js)"
    )


# ── cURL (bash / cmd) ──────────────────────────────────────────────────────────

def _normalize_bash(text: str) -> str:
    return re.sub(r'\\\n\s*', ' ', text).strip()

def _normalize_cmd(text: str) -> str:
    return re.sub(r'\s*\^\s*\n\s*', ' ', text).strip()

def _extract_curl_url(text: str) -> str:
    for pattern in [
        r"curl\s+'([^']+)'",
        r'curl\s+"([^"]+)"',
        r"curl\s+(https?://\S+)",
    ]:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    raise ValueError("Cannot find URL in cURL command")

def _extract_curl_body(text: str) -> Optional[str]:
    patterns = [
        r"--data-raw\s+\$'(.*?)'(?=\s+-|\s*$)",
        r"--data-raw\s+'(.*?)'(?=\s+-|\s*$)",
        r'--data-raw\s+"((?:[^"\\]|\\.)*)"(?=\s+-|\s*$)',
        r"(?:--data|-d)\s+\$'(.*?)'(?=\s+-|\s*$)",
        r"(?:--data|-d)\s+'(.*?)'(?=\s+-|\s*$)",
        r'(?:--data|-d)\s+"((?:[^"\\]|\\.)*)"(?=\s+-|\s*$)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            raw = m.group(1)
            return raw.replace('\\"', '"')
    return None

def _parse_curl(text: str, normalize_fn) -> Tuple[str, Optional[str]]:
    normalized = normalize_fn(text)
    return _extract_curl_url(normalized), _extract_curl_body(normalized)


# ── PowerShell ─────────────────────────────────────────────────────────────────

def _parse_powershell(text: str) -> Tuple[str, Optional[str]]:
    # Join backtick line continuations
    text = re.sub(r'\s*`\s*\n\s*', ' ', text)

    uri_match = re.search(r'-Uri\s+["\']([^"\']+)["\']', text, re.IGNORECASE)
    if not uri_match:
        raise ValueError("Cannot find -Uri in PowerShell command")

    url = uri_match.group(1)

    body_match = re.search(r'-Body\s+["\']([^"\']*)["\']', text, re.IGNORECASE | re.DOTALL)
    body = body_match.group(1) if body_match else None

    return url, body


# ── fetch (browser / Node.js) ──────────────────────────────────────────────────

def _parse_fetch(text: str) -> Tuple[str, Optional[str]]:
    url_match = re.search(r'fetch\s*\(\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
    if not url_match:
        raise ValueError("Cannot find URL in fetch() call")

    url = url_match.group(1)

    # body is a JSON-encoded string inside the options object
    body_match = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if not body_match:
        body_match = re.search(r"'body'\s*:\s*'((?:[^'\\]|\\.)*)'", text)

    body = None
    if body_match:
        body = body_match.group(1).replace('\\"', '"').replace("\\'", "'")

    return url, body


# ── Body parsers ───────────────────────────────────────────────────────────────

def _parse_body(body: str) -> dict:
    # JSON object?
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items()}
    except (json.JSONDecodeError, ValueError):
        pass

    # multipart?
    if 'name="' in body:
        return _parse_multipart(body)

    # URL-encoded fallback
    return dict(urllib.parse.parse_qsl(body))

def _parse_multipart(raw: str) -> dict:
    # Normalize literal \r\n sequences from bash $'...' strings
    normalized = raw.replace('\\r\\n', '\r\n').replace('\\n', '\n')
    pattern = r'name="([^"]+)"\r?\n\r?\n(.*?)(?:\r?\n--|\Z)'
    matches = re.findall(pattern, normalized, re.DOTALL)
    return {key: value.strip() for key, value in matches}


# ── Main converter ─────────────────────────────────────────────────────────────

def text_to_url(text: str) -> Tuple[str, str]:
    """Returns (converted_url, format_label)."""
    fmt = detect_format(text)

    if fmt == 'curl_bash':
        url, body = _parse_curl(text, _normalize_bash)
    elif fmt == 'curl_cmd':
        url, body = _parse_curl(text, _normalize_cmd)
    elif fmt == 'powershell':
        url, body = _parse_powershell(text)
    else:
        url, body = _parse_fetch(text)

    if body:
        params = _parse_body(body)
        if params:
            return f"{url}?{urllib.parse.urlencode(params)}", FORMAT_LABELS[fmt]

    return url, FORMAT_LABELS[fmt]


# ── GUI ────────────────────────────────────────────────────────────────────────

class CurlConverter(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vitalii's cURL → GET Converter v.1.01")
        self.resize(1100, 800)

        layout = QVBoxLayout()

        input_title = QLabel("Paste request from DevTools here")
        input_title.setFont(QFont("Segoe UI", 14))
        layout.addWidget(input_title)

        hint = QLabel(
            "Supported: cURL (bash)  •  cURL (cmd)  •  PowerShell  •  fetch (JS / Node.js)"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(hint)

        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("Paste copied request here...")
        layout.addWidget(self.input_box)

        btn_row = QHBoxLayout()
        convert_btn = QPushButton("Convert  (Ctrl+Enter)")
        convert_btn.clicked.connect(self.convert)
        btn_row.addWidget(convert_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_all)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

        self.detected_label = QLabel("")
        self.detected_label.setStyleSheet("color: #2a7; font-size: 11px;")
        layout.addWidget(self.detected_label)

        output_title = QLabel("Converted URL")
        output_title.setFont(QFont("Segoe UI", 14))
        layout.addWidget(output_title)

        self.output_box = QTextEdit()
        self.output_box.setReadOnly(True)
        self.output_box.setStyleSheet("background-color: #f5f5f5;")
        layout.addWidget(self.output_box)

        self.copy_btn = QPushButton("Copy Result  (Ctrl+Shift+C)")
        self.copy_btn.clicked.connect(self.copy_result)
        layout.addWidget(self.copy_btn)

        self.setLayout(layout)

        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.convert)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self).activated.connect(self.copy_result)

    def convert(self):
        text = self.input_box.toPlainText().strip()
        if not text:
            return
        try:
            result, fmt_label = text_to_url(text)
            self.output_box.setPlainText(result)
            self.detected_label.setText(f"Detected: {fmt_label}")
        except Exception as e:
            self.detected_label.setText("")
            QMessageBox.critical(self, "Error", str(e))

    def clear_all(self):
        self.input_box.clear()
        self.output_box.clear()
        self.detected_label.setText("")

    def copy_result(self):
        text = self.output_box.toPlainText()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.copy_btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("Copy Result  (Ctrl+Shift+C)"))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("cURL to GET")
    app.setApplicationDisplayName("cURL to GET")
    app.setWindowIcon(QIcon(resource_path("icon.ico")))
    window = CurlConverter()
    window.show()
    sys.exit(app.exec())
