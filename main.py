# main.py

import sys
import os
import re
import urllib.parse

from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QLabel,
    QTextEdit,
    QPushButton,
    QMessageBox,
)

from PySide6.QtGui import (
    QFont,
    QIcon,
)


# ==========================================
# RESOURCE PATH
# Needed for PyInstaller --onefile
# ==========================================

def resource_path(relative_path):

    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


# ==========================================
# MULTIPART PARSER
# ==========================================

def parse_multipart(data_raw: str):

    result = {}

    pattern = r'name="([^"]+)"\\r\\n\\r\\n(.*?)\\r\\n'

    matches = re.findall(
        pattern,
        data_raw,
        re.DOTALL
    )

    for key, value in matches:

        value = value.strip()

        result[key] = value

    return result


# ==========================================
# CURL -> URL
# ==========================================

def curl_to_url(curl_text: str):

    # URL
    url_match = re.search(
        r"curl '([^']+)'",
        curl_text
    )

    if not url_match:
        raise ValueError(
            "Cannot find URL inside cURL"
        )

    base_url = url_match.group(1)

    # BODY
    data_match = re.search(
        r"--data-raw \$'(.*?)'",
        curl_text,
        re.DOTALL
    )

    params = {}

    if data_match:

        raw_data = data_match.group(1)

        params = parse_multipart(
            raw_data
        )

    query = urllib.parse.urlencode(params)

    if query:
        return f"{base_url}?{query}"

    return base_url


# ==========================================
# MAIN WINDOW
# ==========================================

class CurlConverter(QWidget):

    def __init__(self):

        super().__init__()

        self.setWindowTitle(
            "cURL (Bash) → getRequest Vitalii`s converter"
        )

        self.resize(1100, 750)

        layout = QVBoxLayout()

        # TITLE
        title = QLabel(
            "Paste cURL (bash) from DevTools here"
        )

        title.setFont(
            QFont("Segoe UI", 14)
        )

        layout.addWidget(title)

        # INPUT
        self.input_box = QTextEdit()

        self.input_box.setPlaceholderText(
            "Paste Copy as cURL (bash) here..."
        )

        layout.addWidget(self.input_box)

        # CONVERT BUTTON
        convert_btn = QPushButton(
            "Convert to URL"
        )

        convert_btn.clicked.connect(
            self.convert
        )

        layout.addWidget(convert_btn)

        # OUTPUT TITLE
        output_title = QLabel(
            "Converted URL"
        )

        output_title.setFont(
            QFont("Segoe UI", 14)
        )

        layout.addWidget(output_title)

        # OUTPUT
        self.output_box = QTextEdit()

        self.output_box.setReadOnly(True)

        layout.addWidget(self.output_box)

        # COPY BUTTON
        copy_btn = QPushButton(
            "Copy Result"
        )

        copy_btn.clicked.connect(
            self.copy_result
        )

        layout.addWidget(copy_btn)

        self.setLayout(layout)

    # ======================================

    def convert(self):

        curl_text = self.input_box.toPlainText()

        try:

            result = curl_to_url(
                curl_text
            )

            self.output_box.setPlainText(
                result
            )

        except Exception as e:

            QMessageBox.critical(
                self,
                "Error",
                str(e)
            )

    # ======================================

    def copy_result(self):

        QApplication.clipboard().setText(
            self.output_box.toPlainText()
        )


# ==========================================
# APP START
# ==========================================

if __name__ == "__main__":

    app = QApplication(sys.argv)

    # WINDOW ICON
    app.setWindowIcon(
        QIcon(resource_path("icon.ico"))
    )

    window = CurlConverter()

    window.show()

    sys.exit(app.exec())