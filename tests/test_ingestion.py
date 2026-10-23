from utilities.ingestion import ingest_user_inputs

sample_inputs = [
    "./inputs/data.pdf"
]

if __name__ == "__main__":
    results = ingest_user_inputs(sample_inputs)
    print("\nIngestion Completed! Output Mapping:")
    print(results)