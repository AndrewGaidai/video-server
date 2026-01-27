#!/bin/bash
source /app/.venv/bin/activate
python video_server.py
```

## **Update `Procfile`:**
```
web: bash start.sh