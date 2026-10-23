from utilities.ingestion import ingest_user_inputs

sample_inputs = [
    "./inputs/data.csv",
    "./inputs/data.docx",
    "./inputs/data.png",
    "./inputs/data.txt",
]

if __name__ == "__main__":
    results = ingest_user_inputs(sample_inputs)
    # A second upload in the same chat reuses the session id
    results = ingest_user_inputs(sample_inputs[:1], results["session_id"])
    print("\nIngestion Completed! Output Mapping:")
    print(results)