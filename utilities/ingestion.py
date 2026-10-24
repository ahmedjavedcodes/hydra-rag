import os
import shutil
import sys
from typing import List, Optional, Union

# Ensure root folder is always in Python's search path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from utilities.session import get_session_dir, new_session_id

# Standard extension mappings
FOLDER_MAPPINGS = {
    "images": [".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp"],
    "spreadsheets": [".csv", ".xlsx", ".xls"],
    "documents": [".pdf", ".docx", ".doc", ".pptx", ".txt", ".rtf"],
}


def category_for(filename: str) -> str:
    """Returns the subfolder (images, spreadsheets, documents, others) for a filename."""
    ext = os.path.splitext(filename)[1].lower()
    for folder_name, extensions in FOLDER_MAPPINGS.items():
        if ext in extensions:
            return folder_name
    return "others"


def unique_destination(directory: str, filename: str) -> str:
    """Claims a path in `directory` that doesn't overwrite an existing file (name.ext -> name_1.ext).

    The file is created empty with an exclusive open, so two uploads at the same moment
    can never get the same name.
    """
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 1
    while True:
        try:
            with open(candidate, "xb"):
                return candidate
        except FileExistsError:
            candidate = os.path.join(directory, f"{stem}_{counter}{ext}")
            counter += 1


def store_file(source_path: str, root_dir: str, filename: Optional[str] = None) -> str:
    """Copies a file into `root_dir/<category>/` without overwriting anything; returns the stored path."""
    filename = os.path.basename(filename or source_path)
    destination_dir = os.path.join(root_dir, category_for(filename))
    os.makedirs(destination_dir, exist_ok=True)
    destination_path = unique_destination(destination_dir, filename)
    try:
        shutil.copyfile(source_path, destination_path)
    except OSError:
        os.remove(destination_path)
        raise
    return destination_path


def organize_files(file_paths: List[str], session_id: str) -> str:
    """Organizes local user input files into categorized subfolders (images, spreadsheets, documents)

    inside the session's own folder `./data/<session_id>/`.
    """
    if not file_paths:
        print("⚠ No files provided to organize.")
        return ""

    session_folder = get_session_dir(session_id)

    processed_count = 0

    for file_path in file_paths:
        file_path_str = str(file_path).strip()

        if not os.path.isfile(file_path_str):
            print(f" ⚠ File not found, skipping: {file_path_str}")
            continue

        filename = os.path.basename(file_path_str)
        destination_path = store_file(file_path_str, session_folder)

        print(
            f" ✓ Copied file '{filename}' -> {os.path.relpath(destination_path)}"
        )
        processed_count += 1

    print(
        f"\n📁 File organization completed. Total files processed: {processed_count}"
    )
    return session_folder


def ingest_user_inputs(
    inputs: Union[str, List[str]], session_id: Optional[str] = None
) -> dict:
    """Copies files into the session's folder without extracting anything.

    For full ingestion (limits, loaders, elements, per-file status) use
    `src.pipeline.ingest.ingest_files`.
    Pass the same `session_id` for later uploads in the same chat; omit it to start a new session.
    """
    if isinstance(inputs, str):
        inputs = [inputs]

    session_id = session_id or new_session_id()
    results = {"session_id": session_id, "uploaded_files_folder": None}

    if inputs:
        print(f"\n📄 Organizing {len(inputs)} local input file(s)...")
        results["uploaded_files_folder"] = organize_files(inputs, session_id)

    return results


def run_ingestion(
    inputs: Union[str, List[str]], session_id: Optional[str] = None
) -> dict:
    """Wrapper function to trigger the complete ingestion pipeline."""
    return ingest_user_inputs(inputs, session_id)
