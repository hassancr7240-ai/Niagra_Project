"""Pre-download IBM Docling ML models into the Docker image layer."""
import sys

try:
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    StandardPdfPipeline.download_models_hf(force=True)
    print("Docling models pre-downloaded successfully")
except Exception as exc:
    print(f"WARNING: Docling model pre-download failed — will download on first use: {exc}")
    sys.exit(0)
