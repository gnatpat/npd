#!/bin/bash
cd ~/pokemon/server
COLLECTION_PASSWORD=<redacted-see-server> /home/nathan/.local/bin/uv run uvicorn main:app --host 0.0.0.0 --port 8151