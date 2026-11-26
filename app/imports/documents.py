import math
import os
import re
import subprocess
import time
from contextlib import closing
from io import BytesIO
from pathlib import PurePosixPath
from unicodedata import normalize
from zipfile import BadZipFile, ZipFile

import pypdfium2 as pdfium
from defusedxml import ElementTree as xml
from defusedxml.common import DefusedXmlException
from PIL import Image, ImageOps, UnidentifiedImageError

from app.imports.errors import ImportProblem


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 30
MAX_CHARS = 200_000
MAX_DOCX_UNPACKED = 25 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_IMPORT_SECONDS = 180
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
EXTENSIONS = {".txt": "txt", ".pdf": "pdf", ".docx": "docx", ".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png"}


def clean_text(value: str) -> str:
    value = normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def detect_format(filename: str, data: bytes) -> str:
    if not data:
        raise ImportProblem("UNREADABLE_FILE", "Файл пуст.", filename=filename)
    if len(data) > MAX_FILE_BYTES:
        raise ImportProblem("FILE_TOO_LARGE", "Максимальный размер файла — 10 МБ.", filename=filename)
    if data.startswith(b"%PDF-"):
        actual = "pdf"
    elif data.startswith(b"PK\x03\x04"):
        actual = "docx"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        actual = "jpeg"
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ImportProblem("UNSUPPORTED_FILE", "Поддерживаются TXT, PDF, DOCX, JPEG и PNG.",
                                filename=filename) from None
        if any(ord(ch) < 32 and ch not in "\t\r\n\f" for ch in text):
            raise ImportProblem("UNSUPPORTED_FILE", "Нужен текстовый файл в UTF-8.", filename=filename)
        actual = "txt"
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix and EXTENSIONS.get(suffix) != actual:
        raise ImportProblem("UNSUPPORTED_FILE", "Расширение файла не соответствует его содержимому.",
                            filename=filename)
    return actual


def read_pages(filename: str, data: bytes, *, deadline: float) -> list[dict]:
    kind = detect_format(filename, data)
    if kind == "txt":
        pages = data.decode("utf-8-sig").split("\f")
        check_page_count(len(pages), filename)
        result = [page_record("page", i, value) for i, value in enumerate(pages, 1) if clean_text(value)]
    elif kind == "docx":
        result = read_docx(filename, data)
    elif kind == "pdf":
        result = read_pdf(filename, data, deadline)
    else:
        result = [page_record("page", 1, read_image(filename, data, deadline))]
    if not result:
        raise ImportProblem("UNREADABLE_FILE", "В файле не найден текст.", filename=filename)
    if sum(len(row["text"]) for row in result) > MAX_CHARS:
        raise ImportProblem("TEXT_LIMIT", "Превышен лимит в 200 000 извлечённых знаков.", filename=filename)
    return result


def page_record(kind: str, number: int, value: str) -> dict:
    return {"locator": {kind: number}, "text": clean_text(value)}


def check_page_count(count: int, filename: str) -> None:
    if count > MAX_PAGES:
        raise ImportProblem("TOO_MANY_PAGES", "В файле может быть не больше 30 страниц.", filename=filename)


def read_docx(filename: str, data: bytes) -> list[dict]:
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2048 or sum(file.file_size for file in entries) > MAX_DOCX_UNPACKED:
                raise ImportProblem("DOCX_UNPACK_LIMIT", "Документ слишком велик после распаковки.", filename=filename)
            if "[Content_Types].xml" not in archive.namelist() or "word/document.xml" not in archive.namelist():
                raise ImportProblem("UNSUPPORTED_FILE", "Архив не является документом DOCX.", filename=filename)
            root = xml.fromstring(archive.read("word/document.xml"))
            if root.tag != W + "document":
                raise ImportProblem("UNSUPPORTED_FILE", "Содержимое не является документом DOCX.", filename=filename)
            declared = 0
            if "docProps/app.xml" in archive.namelist():
                props = xml.fromstring(archive.read("docProps/app.xml"))
                count = next((child.text for child in props if child.tag.endswith("}Pages")), "")
                if count and count.isascii() and count.isdecimal() and len(count) < 12:
                    declared = int(count)
    except (BadZipFile, OSError, KeyError, RuntimeError, xml.ParseError, DefusedXmlException):
        raise ImportProblem("UNREADABLE_FILE", "Не удалось прочитать DOCX.", filename=filename) from None

    explicit = sum(node.get(W + "type") == "page" for node in root.iter(W + "br"))
    rendered = sum(1 for _ in root.iter(W + "lastRenderedPageBreak"))
    check_page_count(max(declared, explicit + 1, rendered + 1), filename)
    records = []
    for i, paragraph in enumerate(root.iter(W + "p"), 1):
        pieces = []
        for part in paragraph.iter():
            if part.tag == W + "t":
                pieces.append(part.text or "")
            elif part.tag in {W + "tab", W + "br", W + "cr"}:
                pieces.append("\n" if part.tag != W + "tab" else " ")
        text = clean_text("".join(pieces))
        if text:
            records.append({"locator": {"paragraph": i}, "text": text})
    return records


def read_pdf(filename: str, data: bytes, deadline: float) -> list[dict]:
    section = None
    try:
        with closing(pdfium.PdfDocument(data)) as pdf:
            check_page_count(len(pdf), filename)
            rows = []
            total = 0
            for i in range(len(pdf)):
                section = i + 1
                remaining_time(deadline, filename, section)
                with closing(pdf[i]) as page:
                    with closing(page.get_textpage()) as textpage:
                        text = clean_text(textpage.get_text_bounded())
                    if not text:
                        width, height = page.get_size()
                        if width <= 0 or height <= 0 or not math.isfinite(width * height):
                            raise ImportProblem("UNREADABLE_FILE", "Некорректный размер страницы.",
                                                filename=filename, page=section)
                        scale = min(2.5, math.sqrt(MAX_PIXELS / (width * height)))
                        if max(width, height) * scale > 12_000:
                            scale = 12_000 / max(width, height)
                        with closing(page.render(scale=scale)) as bitmap:
                            text = ocr(bitmap.to_pil(), filename, section, deadline)
                total += len(text)
                if total > MAX_CHARS:
                    raise ImportProblem("TEXT_LIMIT", "Превышен лимит в 200 000 извлечённых знаков.",
                                        filename=filename, page=section)
                rows.append({"locator": {"page": section}, "text": text})
            return rows
    except (pdfium.PdfiumError, ValueError, OSError):
        raise ImportProblem("UNREADABLE_FILE", "Не удалось прочитать PDF.", filename=filename, page=section) from None


def read_image(filename: str, data: bytes, deadline: float) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ImportProblem("IMAGE_TOO_LARGE", "Уменьшите разрешение изображения.", filename=filename, page=1)
            return ocr(ImageOps.exif_transpose(image), filename, 1, deadline)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise ImportProblem("UNREADABLE_FILE", "Не удалось прочитать изображение.", filename=filename, page=1) from None


def remaining_time(deadline: float, filename: str, page: int) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise ImportProblem("IMPORT_TIMEOUT", "Импорт занял слишком много времени. Разделите файлы.",
                            filename=filename, page=page)
    return left


def ocr(image: Image.Image, filename: str, page: int, deadline: float) -> str:
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    executable = os.getenv("TESSERACT_BIN", "tesseract")
    try:
        process = subprocess.run(
            [executable, "stdin", "stdout", "-l", "rus+eng", "--psm", "3"],
            input=buffer.getvalue(), capture_output=True, timeout=min(15, remaining_time(deadline, filename, page)),
            check=True,
        )
    except subprocess.TimeoutExpired:
        raise ImportProblem("OCR_TIMEOUT", "Распознавание страницы заняло слишком много времени.",
                            filename=filename, page=page) from None
    except (OSError, subprocess.CalledProcessError):
        raise ImportProblem("OCR_FAILED", "Не удалось запустить распознавание страницы.",
                            filename=filename, page=page) from None
    text = clean_text(process.stdout.decode("utf-8", "replace"))
    if not text:
        raise ImportProblem("UNREADABLE_FILE", "Текст не распознан. Загрузите чёткую страницу или вставьте текст.",
                            filename=filename, page=page)
    return text
