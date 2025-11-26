#!/bin/bash

# Install Chromium in the current runtime container
python -m playwright install chromium --with-deps

# Start FastAPI
uvicorn main:app --host 0.0.0.0 --port 8080
