import requests

import hashlib
import os
import re
import zipfile
import tarfile
import gzip
from pathlib import Path


CHUNK_SIZE = 1024 * 1024

SUSPICIOUS_EXTENSIONS = {
    ".exe",
    ".msi",
    ".scr",
    ".bat",
    ".cmd",
    ".com",
    ".ps1",
    ".vbs",
    ".vbe",
    ".js",
    ".jse",
    ".wsf",
    ".wsh",
    ".hta",
    ".dll",
}

SCRIPT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".vbe",
    ".js",
    ".jse",
    ".wsf",
    ".wsh",
    ".hta",
}

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
}

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
}

DOUBLE_EXTENSION_PATTERN = re.compile(
    r"\.(pdf|doc|docx|xls|xlsx|jpg|jpeg|png|txt|zip|rar|mp3|mp4)"
    r"\.(exe|scr|bat|cmd|com|msi|js|vbs|ps1)$",
    re.IGNORECASE
)


def send_to_dashboard(result):
    try:
        requests.post(
            "http://127.0.0.1:5000/event",
            json={
                "source": "security",
                "event": "security_scan",
                "data": result
            },
            timeout=2
        )

    except requests.RequestException as error:
        print(
            f"Could not connect to DDAS bridge: {error}"
        )


def calculate_sha256(file_path):
    """
    Calculate the SHA-256 hash of a file.
    """

    hasher = hashlib.sha256()

    try:
        with open(file_path, "rb") as file:
            while True:
                chunk = file.read(CHUNK_SIZE)

                if not chunk:
                    break

                hasher.update(chunk)

        return hasher.hexdigest()

    except (
        PermissionError,
        FileNotFoundError,
        OSError
    ):
        return None


def get_extension(file_path):
    """
    Return the lowercase file extension.
    """

    return Path(file_path).suffix.lower()


def check_double_extension(file_path):
    """
    Detect filenames such as:
        invoice.pdf.exe
        photo.jpg.scr
    """

    file_name = os.path.basename(file_path)

    return bool(
        DOUBLE_EXTENSION_PATTERN.search(file_name)
    )


def check_suspicious_extension(file_path):
    """
    Check whether the file type can execute code
    or contain executable scripts.
    """

    extension = get_extension(file_path)

    if extension in SUSPICIOUS_EXTENSIONS:
        return True

    return False


def check_archive_integrity(file_path):
    """
    Validate common archive formats.

    Returns:
        True  -> archive appears valid
        False -> archive appears corrupted
        None  -> format could not be checked
    """

    extension = get_extension(file_path)

    try:

        if extension == ".zip":

            with zipfile.ZipFile(file_path, "r") as archive:

                bad_file = archive.testzip()

                return bad_file is None

        if extension in {".tar", ".tgz"}:

            with tarfile.open(file_path, "r:*") as archive:

                archive.getmembers()

                return True

        if extension == ".gz":

            with gzip.open(file_path, "rb") as archive:

                while archive.read(CHUNK_SIZE):
                    pass

                return True

    except (
        zipfile.BadZipFile,
        tarfile.TarError,
        gzip.BadGzipFile,
        EOFError,
        OSError,
        ValueError
    ):

        return False

    return None


def check_image_integrity(file_path):
    """
    Basic image integrity check.

    Uses Pillow if installed.
    If Pillow is not installed, the check is skipped.
    """

    extension = get_extension(file_path)

    if extension not in IMAGE_EXTENSIONS:
        return None

    try:

        from PIL import Image

    except ImportError:

        return None

    try:

        with Image.open(file_path) as image:

            image.verify()

        return True

    except Exception:

        return False


def check_file_header(file_path):
    """
    Perform basic file signature checks.

    This is not a malware scanner.
    It only verifies some common file structures.
    """

    extension = get_extension(file_path)

    try:

        with open(file_path, "rb") as file:

            header = file.read(16)

    except (
        PermissionError,
        FileNotFoundError,
        OSError
    ):

        return None

    if extension in {".exe", ".scr", ".dll"}:

        return header[:2] == b"MZ"

    if extension == ".pdf":

        return header.startswith(b"%PDF")

    if extension == ".zip":

        return (
            header.startswith(b"PK\x03\x04")
            or header.startswith(b"PK\x05\x06")
            or header.startswith(b"PK\x07\x08")
        )

    if extension == ".png":

        return header.startswith(
            b"\x89PNG\r\n\x1a\n"
        )

    if extension in {".jpg", ".jpeg"}:

        return header.startswith(b"\xff\xd8\xff")

    if extension == ".gif":

        return header.startswith(
            (b"GIF87a", b"GIF89a")
        )

    return None


def check_empty_file(file_path):
    """
    Detect a zero-byte file.
    """

    try:
        return os.path.getsize(file_path) == 0

    except (
        FileNotFoundError,
        PermissionError,
        OSError
    ):

        return False


def analyze_file(file_path):
    """
    Perform the complete DDAS security analysis.

    Returns a dictionary containing:

        status
        risk
        reason
        corrupted
        sha256
        size
        extension
    """

    result = {
        "file": os.path.abspath(file_path),
        "name": os.path.basename(file_path),
        "status": "SAFE",
        "risk": "LOW",
        "reason": "No suspicious indicators detected.",
        "corrupted": False,
        "sha256": None,
        "size": 0,
        "extension": get_extension(file_path),
        "checks": []
    }

    if not os.path.isfile(file_path):

        result["status"] = "ERROR"
        result["risk"] = "UNKNOWN"
        result["reason"] = "File does not exist."

        return result

    try:

        result["size"] = os.path.getsize(
            file_path
        )

    except OSError:

        result["status"] = "ERROR"
        result["risk"] = "UNKNOWN"
        result["reason"] = "Unable to read file information."

        return result

    if check_empty_file(file_path):

        result["status"] = "SUSPICIOUS"
        result["risk"] = "MEDIUM"
        result["reason"] = "File is empty."
        result["checks"].append(
            "Zero-byte file"
        )

    result["sha256"] = calculate_sha256(
        file_path
    )

    if result["sha256"]:

        result["checks"].append(
            "SHA-256 calculated"
        )

    else:

        result["status"] = "ERROR"
        result["risk"] = "UNKNOWN"
        result["reason"] = "Unable to calculate SHA-256."

        return result

    if check_double_extension(file_path):

        result["status"] = "SUSPICIOUS"
        result["risk"] = "HIGH"
        result["reason"] = (
            "Filename uses a potentially deceptive "
            "double extension."
        )

        result["checks"].append(
            "Deceptive double extension"
        )

    elif check_suspicious_extension(file_path):

        extension = get_extension(file_path)

        if extension in SCRIPT_EXTENSIONS:

            result["status"] = "SUSPICIOUS"
            result["risk"] = "MEDIUM"
            result["reason"] = (
                "File is an executable script type."
            )

            result["checks"].append(
                "Executable script"
            )

        else:

            result["status"] = "SUSPICIOUS"
            result["risk"] = "MEDIUM"
            result["reason"] = (
                "File is an executable file type."
            )

            result["checks"].append(
                "Executable file"
            )

    header_result = check_file_header(
        file_path
    )

    if header_result is False:

        result["status"] = "SUSPICIOUS"
        result["risk"] = "HIGH"
        result["corrupted"] = True
        result["reason"] = (
            "File signature does not match "
            "its extension."
        )

        result["checks"].append(
            "Invalid file signature"
        )

    elif header_result is True:

        result["checks"].append(
            "File signature valid"
        )

    archive_result = check_archive_integrity(
        file_path
    )

    if archive_result is False:

        result["status"] = "SUSPICIOUS"
        result["risk"] = "HIGH"
        result["corrupted"] = True
        result["reason"] = (
            "Archive appears to be corrupted "
            "or incomplete."
        )

        result["checks"].append(
            "Archive integrity failed"
        )

    elif archive_result is True:

        result["checks"].append(
            "Archive integrity passed"
        )

    image_result = check_image_integrity(
        file_path
    )

    if image_result is False:

        result["status"] = "SUSPICIOUS"
        result["risk"] = "HIGH"
        result["corrupted"] = True
        result["reason"] = (
            "Image appears to be corrupted "
            "or unreadable."
        )

        result["checks"].append(
            "Image integrity failed"
        )

    elif image_result is True:

        result["checks"].append(
            "Image integrity passed"
        )

    return result


def print_report(result):
    """
    Display a simple security report in the terminal.
    """

    print()
    print("=" * 55)
    print("DDAS SECURITY REPORT")
    print("=" * 55)

    print(f"File:       {result['name']}")
    print(f"Status:     {result['status']}")
    print(f"Risk:       {result['risk']}")
    print(f"Corrupted:  {result['corrupted']}")
    print(f"Size:       {result['size']} bytes")
    print(f"SHA-256:    {result['sha256']}")
    print(f"Reason:     {result['reason']}")

    print()
    print("Checks:")

    for check in result["checks"]:
        print(f"  ✓ {check}")

    print("=" * 55)
    print()


if __name__ == "__main__":

    print("DDAS Security Engine")
    print("--------------------")

    file_path = input(
        "Enter the path of a file to scan: "
    ).strip().strip('"')

    if not file_path:

        print("No file specified.")

    else:

        result = analyze_file(
            file_path
        )

        print_report(
            result
        )

        send_to_dashboard(result)
