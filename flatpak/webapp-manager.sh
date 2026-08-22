#!/bin/sh
# Flatpak entry point. main.py only runs webapp-manager.py, whose filename
# contains a dash and therefore cannot be imported as a module.
exec python3 /app/share/webapp-manager/main.py "$@"
