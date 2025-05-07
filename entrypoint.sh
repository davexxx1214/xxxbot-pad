#!/bin/bash

# Set working directory to script location
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
cd "$SCRIPT_DIR"

# Log file
LOG_FILE="startup_log.txt"
LOG_DIR="logs"

# Create logs directory if not exists
if [ ! -d "$LOG_DIR" ]; then
    echo "Creating logs directory: $LOG_DIR"
    mkdir -p "$LOG_DIR"
    echo "[$(date)] Created logs directory: $LOG_DIR" >> "$LOG_FILE"
fi

echo "[$(date)] Starting script execution" >> "$LOG_FILE"
echo "======================================"
echo "       Service Startup Script         "
echo "======================================"
echo "Starting...Please wait..."

# Check if screen is installed
if ! command -v screen &> /dev/null; then
    echo "Error: screen is not installed. Please install it first."
    echo "[$(date)] Error: screen is not installed" >> "$LOG_FILE"
    exit 1
fi

# Start Redis service in screen
if ! screen -list | grep -q "redis_service"; then
    echo "Creating Redis screen session..."
    screen -dmS redis_service bash -c '
        cd "'$SCRIPT_DIR'/849/redis" || { echo "Redis directory not found"; exit 1; }
        echo "Starting Redis server with Linux configuration..."
        redis-server redis.linux.conf
        echo "Redis server exited. Press Enter to close this window."
        read
    '
    echo "Redis server started in screen session 'redis_service'"
else
    echo "Redis screen session already exists, skipping..."
fi

# Wait briefly before starting PAD service
sleep 2

# Start PAD service in screen
if ! screen -list | grep -q "pad_service"; then
    echo "Creating PAD screen session..."
    screen -dmS pad_service bash -c '
        cd "'$SCRIPT_DIR'/849/pad2" || { echo "PAD directory not found"; exit 1; }
        echo "Adding execute permission to linuxService..."
        chmod +x linuxService
        echo "Starting PAD service..."
        ./linuxService
        echo "PAD service exited. Press Enter to close this window."
        read
    '
    echo "PAD service started in screen session 'pad_service'"
else
    echo "PAD screen session already exists, skipping..."
fi

# Wait briefly before starting main application
sleep 2

# Start main application in screen
if ! screen -list | grep -q "wxbot_main"; then
    echo "Creating main application screen session..."
    screen -dmS wxbot_main bash -c '
        cd "'$SCRIPT_DIR'" || { echo "Main directory not found"; exit 1; }
        echo "Starting main application..."
        python3 main.py
        echo "Main application exited. Press Enter to close this window."
        read
    '
    echo "Main application started in screen session 'wxbot_main'"
else
    echo "Main app screen session already exists, skipping..."
fi

echo "[$(date)] Script execution completed" >> "$LOG_FILE"
echo ""
echo "All services started in screen sessions:"
echo "- Redis: 'redis_service'"
echo "- PAD: 'pad_service'"
echo "- Main app: 'wxbot_main'"
echo ""
echo "To attach to a session use: screen -r [session_name]"
echo "To detach from a session use: Ctrl+A, D"
echo "To list all sessions use: screen -ls"
