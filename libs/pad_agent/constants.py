"""Shared constants for pad_agent."""

from __future__ import annotations

from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Hong_Kong")
ADB_DEFAULT_PORT = 5555
CYCLE_BUDGET_SECONDS = 10
FIFO_NAME = "in.pad"
PIPE_BUF = 4096
REPORT_EVERY_N_CYCLES = 10  # periodic FIFO report every ~5 minutes
DEVICE_STATE_PATH = "/sdcard/Android/data/de.ozerov.fully/files/kidpad/state.json"
DEVICE_KIDPAD_DIR = "/sdcard/Android/data/de.ozerov.fully/files/kidpad/"
DASHBOARD_VERSION = "3"  # bump to force re-push of dashboard files

# FK REST API is only accessible via ADB port forward (not direct network).
# After `adb forward tcp:<local>:tcp:2323`, use http://127.0.0.1:<local>
FK_REST_LOCAL_PORT = 2323  # local port for ADB forward

# FK REST API
FK_REST_PORT = 2323
FK_CMD_LOAD_URL = "loadURL"
FK_CMD_DEVICE_INFO = "deviceInfo"
FK_CMD_START_APP = "startApplication"
FK_CMD_SET_STRING = "setStringSetting"
FK_CMD_CLEAR_CACHE = "clearCache"
FK_REST_TIMEOUT_SECONDS = 5
FK_REST_MAX_RETRIES = 1
