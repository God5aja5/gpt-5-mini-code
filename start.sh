#!/bin/bash
if [ "$RENDER" ]; then
    # Production with gunicorn
    exec gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app
else
    # Development
    exec python app.py
fi
