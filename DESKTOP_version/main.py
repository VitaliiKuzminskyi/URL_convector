import sys
import os
import re
import urllib.parse

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QMessageBox,
)
from PySide6.QtGui import QFont, QIcon, QPixmap, QMovie, QPainter, QColor
from PySide6.QtCore import Qt, Signal


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


class ConverterWindow(QWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("cURL (bash) → GET request converter v1.1.1")
        self.resize(550, 375)

        layout = QVBoxLayout()

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

        layout.addLayout(title_row)

        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("Paste cURL (bash) type here...")
        layout.addWidget(self.input_box)

        convert_btn = QPushButton("Convert to GET request")
        convert_btn.setStyleSheet(
            "QPushButton { background-color: #a3ffb5; }"
            "QPushButton:hover { background-color: #85e89a; }"
            "QPushButton:pressed { background-color: #63cc7a; }"
        )
        convert_btn.clicked.connect(self.on_convert)
        layout.addWidget(convert_btn)

        output_title = QLabel("Converted URL")
        output_title.setFont(QFont("Segoe UI", 14))
        layout.addWidget(output_title)

        self.output_box = QTextEdit()
        self.output_box.setReadOnly(True)
        layout.addWidget(self.output_box)

        copy_btn = QPushButton("Copy GET request")
        copy_btn.setStyleSheet(
            "QPushButton { background-color: #f0f2af; }"
            "QPushButton:hover { background-color: #d8da8e; }"
            "QPushButton:pressed { background-color: #c0c270; }"
        )
        copy_btn.clicked.connect(self.copy_result)
        layout.addWidget(copy_btn)

        self.setLayout(layout)

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

    def copy_result(self):
        QApplication.clipboard().setText(self.output_box.toPlainText())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path("resources/icon.ico")))
    window = ConverterWindow()
    window.show()
    sys.exit(app.exec())
