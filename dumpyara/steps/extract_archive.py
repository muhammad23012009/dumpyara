#
# SPDX-FileCopyrightText: Dumpyara Project
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Step 1.

This step will extract the archive into a folder.
"""

import re
from pathlib import Path
from re import Pattern, compile
from shutil import unpack_archive
from sebaubuntu_libs.liblogging import LOGD, LOGI
from typing import Callable, Dict
from zipfile import ZipFile, is_zipfile

from dumpyara.utils.files import get_recursive_files_list

try:
    import firmware_parsers

    _HAS_FIRMWARE_PARSERS = True
except ImportError:
    _HAS_FIRMWARE_PARSERS = False


def _strip_vendor_prefix(directory: Path):
    """Strip common vendor prefixes from extracted filenames.

    Detects patterns like Nokia NB0 'LFC-X-YYYY-ZZZZ-name.ext' and renames to 'name.ext'.
    Only acts when a clear majority of files share the same prefix pattern.
    """
    files = [f for f in directory.iterdir() if f.is_file()]
    if len(files) < 3:
        return

    # Match Nokia-style prefix: LFC-{seg}-{seg}-{seg}- or LFC-{seg}-{seg}-
    prefix_pattern = re.compile(r"^[A-Z]{2,4}(?:-[A-Za-z0-9]{1,6}){2,4}-")
    prefixed = {}
    for f in files:
        m = prefix_pattern.match(f.name)
        if m:
            new_name = f.name[m.end() :]
            if new_name and not (directory / new_name).exists():
                prefixed[f] = directory / new_name

    # Only rename if most files have the prefix (avoid false positives)
    if len(prefixed) >= len(files) * 0.6:
        for old, new in prefixed.items():
            LOGD(f"Stripping vendor prefix: {old.name} → {new.name}")
            old.rename(new)


def _has_nested_partition_markers(archive_path: Path) -> bool:
    """Return True when a nested zip contains dumpable partition container markers."""
    if not is_zipfile(archive_path):
        LOGD(f"Skipping nested zip scan for non-zip archive: {archive_path.name}")
        return False

    try:
        with ZipFile(archive_path, "r") as zip_file:
            for file_name in zip_file.namelist():
                for pattern in NESTED_ZIP_PARTITION_MARKERS:
                    if pattern.search(file_name):
                        return True
    except Exception as e:
        LOGD(f"Failed to inspect nested zip {archive_path.name}: {e}")
        return False

    return False


def extract_archive(archive_path: Path, extracted_archive_path: Path, is_nested: bool = False):
    """
    Extract the archive into a folder.
    """
    LOGD(f"Extracting archive: {archive_path.name}")

    # Try firmware_parsers detection first
    if _HAS_FIRMWARE_PARSERS:
        try:
            fmt = firmware_parsers.detect(str(archive_path))
            if fmt != "unknown":
                extractor = getattr(firmware_parsers, fmt, None)
                if extractor is not None:
                    LOGI(f"Detected firmware format: {fmt}")
                    extractor(str(archive_path), str(extracted_archive_path))
                    if is_nested:
                        archive_path.unlink()
                    return
        except Exception as e:
            LOGI(f"firmware_parsers failed ({e}), falling back to generic extraction")

    # Extract the archive
    try:
        unpack_archive(archive_path, extracted_archive_path)
    except Exception:
        # Fallback: try as zip for non-standard extensions (.ozip, .ftf, etc.)
        if is_zipfile(archive_path):
            LOGD(f"Falling back to zipfile for {archive_path.name}")
            with ZipFile(archive_path, "r") as zf:
                zf.extractall(extracted_archive_path)
        else:
            raise
    if is_nested:
        LOGD("Archive is nested, unlinking")
        archive_path.unlink()

    # Flatten the folder
    for file in get_recursive_files_list(extracted_archive_path):
        if file == extracted_archive_path / file.name:
            continue

        file.rename(extracted_archive_path / file.name)

    # Re-detect firmware formats in extracted files
    if _HAS_FIRMWARE_PARSERS:
        for file in list(get_recursive_files_list(extracted_archive_path)):
            try:
                fmt = firmware_parsers.detect(str(file))
                if fmt != "unknown":
                    extractor = getattr(firmware_parsers, fmt, None)
                    if extractor is not None:
                        LOGI(f"Detected nested firmware format: {fmt} in {file.name}")
                        extractor(str(file), str(extracted_archive_path))
                        file.unlink()
            except Exception as e:
                LOGD(f"firmware_parsers failed on {file.name}: {e}")

    # Strip common vendor prefixes from filenames
    # (e.g., Nokia NB0 "LFC-0-1060-00WW-boot.img" -> "boot.img")
    _strip_vendor_prefix(extracted_archive_path)

    # Check for nested archives
    extracted_archive_tempdir_files_list = list(
        get_recursive_files_list(extracted_archive_path, True)
    )
    for pattern, func in NESTED_ARCHIVES.items():
        matches = [
            file for file in extracted_archive_tempdir_files_list if pattern.match(str(file))
        ]

        if not matches:
            LOGI(f"Pattern {pattern.pattern} not found")
            continue

        for file in matches:
            nested_archive = extracted_archive_path / file

            LOGI(f"Found nested archive: {nested_archive.name}")

            if not nested_archive.is_file():
                LOGD(f"Nested archive {nested_archive.name} probably already handled, skipping")
                continue

            func(nested_archive, extracted_archive_path, True)

    nested_archive_patterns = tuple(NESTED_ARCHIVES.keys())
    for file in extracted_archive_tempdir_files_list:
        if any(pattern.match(str(file)) for pattern in nested_archive_patterns):
            continue

        if not NESTED_ZIP_PATTERN.match(str(file)):
            continue

        nested_archive = extracted_archive_path / file
        LOGI(f"Found nested zip candidate: {nested_archive.name}")

        if not nested_archive.is_file():
            LOGD(f"Nested zip {nested_archive.name} probably already handled, skipping")
            continue

        if not _has_nested_partition_markers(nested_archive):
            LOGD(f"Skipping nested zip {nested_archive.name}: no partition markers")
            continue

        extract_archive(nested_archive, extracted_archive_path, True)

    LOGD(f"Extracted archive: {archive_path.name}")


NESTED_ZIP_PARTITION_MARKERS = (
    compile(
        r"(?:^|/)"
        r"(?:boot|boot-debug|boot-verified|cust|dtbo|dtbo-verified|exaid|factory|india|"
        r"init_boot|mi_ext|modem|my_bigball|my_carrier|my_company|my_country|my_custom|"
        r"my_engineering|my_heytap|my_manifest|my_odm|my_operator|my_preload|my_product|"
        r"my_region|my_stock|my_version|NON-HLOS|odm|odm_dlkm|odm_ext|oem|oppo_product|"
        r"opproduct|preas|preavs|preload|preload_common|product|product_h|recovery|rescue|"
        r"reserve|special_preload|super|system|system_dlkm|system_ext|system_other|"
        r"systemex|tz|vendor|vendor_boot|vendor_boot-debug|vendor_dlkm|"
        r"vendor_kernel_boot|xrom)(?:_[ab])?\.new\.dat\.br$"
    ),
    compile(
        r"(?:^|/)"
        r"(?:boot|boot-debug|boot-verified|cust|dtbo|dtbo-verified|exaid|factory|india|"
        r"init_boot|mi_ext|modem|my_bigball|my_carrier|my_company|my_country|my_custom|"
        r"my_engineering|my_heytap|my_manifest|my_odm|my_operator|my_preload|my_product|"
        r"my_region|my_stock|my_version|NON-HLOS|odm|odm_dlkm|odm_ext|oem|oppo_product|"
        r"opproduct|preas|preavs|preload|preload_common|product|product_h|recovery|rescue|"
        r"reserve|special_preload|super|system|system_dlkm|system_ext|system_other|"
        r"systemex|tz|vendor|vendor_boot|vendor_boot-debug|vendor_dlkm|"
        r"vendor_kernel_boot|xrom)(?:_[ab])?\.transfer\.list$"
    ),
    compile(r"(?:^|/)payload\.bin$"),
    compile(r"(?:^|/)super(?!.*(_empty)).*\.img$"),
    compile(r"(?:^|/)[^/]+\.tar\.md5$"),
)
NESTED_ZIP_PATTERN = compile(r".*\.zip$")
NESTED_ARCHIVES: Dict[Pattern[str], Callable[[Path, Path, bool], None]] = {
    compile(key): value
    for key, value in {
        ".*\\.tar\\.md5": extract_archive,
    }.items()
}
