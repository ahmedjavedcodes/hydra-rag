import os
import shutil
import sys
from typing import List, Union

# Ensure root folder is always in Python's search path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Standard extension mappings
FOLDER_MAPPINGS = {
    "images": [".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp"],
    "spreadsheets": [".csv", ".xlsx", ".xls"],
    "documents": [".pdf", ".docx", ".doc", ".txt", ".rtf"],
}


def organize_files(file_paths: List[str], base_output_dir: str = "./data") -> str:
    """Organizes local user input files into categorized subfolders (images, spreadsheets, documents)

    within a unified target root folder inside `./data`.
    """
    if not file_paths:
        print("⚠ No files provided to organize.")
        return ""

    # Create root folder for this batch ingestion session
    session_folder = os.path.normpath(
        os.path.join(base_output_dir, "user_uploads")
    )
    os.makedirs(session_folder, exist_ok=True)

    processed_count = 0

    for file_path in file_paths:
        file_path_str = str(file_path).strip()

        if not os.path.isfile(file_path_str):
            print(f" ⚠ File not found, skipping: {file_path_str}")
            continue

        filename = os.path.basename(file_path_str)
        _, ext = os.path.splitext(filename)
        ext = ext.lower()

        # Determine subfolder based on extension
        target_subfolder = "others"
        for folder_name, extensions in FOLDER_MAPPINGS.items():
            if ext in extensions:
                target_subfolder = folder_name
                break

        # Construct destination directory & copy file
        destination_dir = os.path.join(session_folder, target_subfolder)
        os.makedirs(destination_dir, exist_ok=True)

        destination_path = os.path.join(destination_dir, filename)
        shutil.copy2(file_path_str, destination_path)

        print(
            f" ✓ Moved file '{filename}' -> {os.path.relpath(destination_path)}"
        )
        processed_count += 1

    print(
        f"\n📁 File organization completed. Total files processed: {processed_count}"
    )
    return session_folder


def ingest_user_inputs(inputs: Union[str, List[str]]) -> dict:
    """Unified entry point for HydraRAG file ingestion.

    Accepts single or multiple file paths and organizes them into structured folders.
    """
    if isinstance(inputs, str):
        inputs = [inputs]

    results = {"uploaded_files_folder": None}

    if inputs:
        print(f"\n📄 Organizing {len(inputs)} local input file(s)...")
        uploads_path = organize_files(inputs)
        results["uploaded_files_folder"] = uploads_path

    return results


def run_ingestion(inputs: Union[str, List[str]]) -> dict:
    """Wrapper function to trigger the complete ingestion pipeline."""
    return ingest_user_inputs(inputs)