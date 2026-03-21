"""
GreymatterAI — Live Run Entry Point
Usage:
    python run_live.py
    or via uvicorn directly:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import subprocess
import sys

if __name__ == "__main__":
    subprocess.run(
        [
            sys.executable, "-m", "uvicorn",
            "main:app",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--log-level", "info",
        ],
        check=True,
    )
