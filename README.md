# PondEyes

**PondEyes** is a Python-based visualization tool for real-time tracking of human position and motion using low-cost millimeter-wave radar modules such as the **Hi-Link HLK-LD2450**. It runs on macOS and Linux, using **PyGame** to render a top-down map view of a physical space (e.g., a room or hallway) and displays moving targets based on radar data. The system supports both **Serial (UART)** and **MQTT** input, logs all targets to CSV, and includes a full playback GUI for reviewing recorded sessions.

---

## Overview

PondEyes provides a lightweight, real-time 2D radar visualization layer suitable for research, demonstration, and educational use.  
It is designed around modular components, each in the `radar/` directory, with a launcher (`main.py`) at the project root.

---

## Features

- Tracks up to three live targets (hardware-limited)
- Custom SVG space maps for accurate placement
- Serial (UART) and MQTT input modes
- Adjustable motion smoothing and trail duration
- Distance-based audible alerts and color-coded velocity display
- Target logging with CSV output for analysis and playback
- Full playback window for reviewing tracked movement

---
PondEyes Development Demonstration (YouTube):
[![Watch the video](https://img.youtube.com/vi/FxQKXyqbS6g/maxresdefault.jpg)](https://youtu.be/FxQKXyqbS6g)

---

## Directory Structure

```
PondEyes/
├── main.py
├── radar/
│   ├── config.py           # Loads and saves radar_config.json with defaults
│   ├── constants.py        # Global constants, colors, and font setup
│   ├── gui.py              # Live visualization GUI (PyGame main window)
│   ├── mqtt_client.py      # MQTT frame receiver and parser
│   ├── playback_gui.py     # CSV track playback GUI
│   ├── serial_reader.py    # Serial interface reader for LD2450/HLK-LD2450
│   ├── sound.py            # Distance and velocity-based audio tones
│   ├── svg_utils.py        # SVG rasterization and coordinate fitting
│   ├── tracking.py         # Target bookkeeping and per-track CSV writing
│   └── config.json         # Generated runtime configuration file
└── logs/                   # Automatically created per-day CSV logs
```

---

## Installation

### Prerequisites

- Python 3.10 or newer  
- PyGame ≥ 2.5  
- CairoSVG  
- Paho-MQTT (if using MQTT mode)  
- pySerial (if using UART/Serial mode)

### Installation Commands

```bash
git clone https://github.com/davidkarnowski/PondEyes.git
cd PondEyes
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Hardware: 
The following hardware chains were used in the development of this project:

Direct Serial Connection:
HLK-LD2450 -> FT232 UART to USB Adapter -> MacOS 15 & Ubuntu 24.04 LTS

MQTT Connection:
HLK-LD2450 -> FT232 UART to USB Adapter -> Raspberry Pi Zero -> LAN

### Hi-Link HLK-LD2450 24 Ghz MM-Wave Radar Module

| Specification | Value |
|----------------|-------|
| Frequency Band | 24 GHz ISM |
| Ranging Distance | 6–8 m |
| View Angle | 60° ± Azimuth, 45° ± Elevation |
| Interface | UART (default 256,000 baud) |
| MCU | Internal processing; transmits processed targets |
| Output Format | 30-byte binary frame per update |

This module provides position (X, Y) and velocity data for up to three simultaneous targets.  
It can penetrate non-metallic materials and operate under variable lighting conditions.

Product page: [https://www.hlktech.net/index.php?id=1157](https://www.hlktech.net/index.php?id=1157)

### DSD TECH SH-U09C USB to TTL Serial Adapter with FTDI FT232RL Chip

For direct serial connection testing a UART to USB (TTL) adapter was used and connected to three different systems including an M1 Macbook Pro, Acer Chromebook (running Ubuntu 24.04 LTS) and a Raspberry Pi Zero

Product page: [https://www.amazon.com/dp/B07BBPX8B8](https://www.deshide.com/product-details_SH-U09C.html)

---

## Configuration

Configuration is managed through `radar/config.py` and stored as a JSON file (`radar_config.json`) in the project root.

Example configuration:

```json
{
  "map": "map.svg",
  "sensor": [0.0, 0.0],
  "heading": 0.0,
  "input_mode": "mqtt",
  "serial_port": "/dev/ttyUSB0",
  "serial_baud": 256000,
  "broker": "127.0.0.1",
  "port": 1883,
  "topic": "PondEyes/raw",
  "trail_duration": 5.0,
  "trail_on": true,
  "smoothing_on": true,
  "smooth_level": 5
}
```

The default configuration file is automatically created or updated from within the GUI configuration screen.

---

## Usage

### 1. Launch Application

```bash
python3 main.py
```

### 2. Select Input Source

- **Serial**: Connect radar via USB/UART.  
  Set `/dev/ttyUSB0` (Linux) or `/dev/tty.usbserial*` (macOS).
- **MQTT**: Enter broker IP, port, and topic (default: `PondEyes/raw`).

### 3. Set Sensor Position and Angle

Using the configuration menu, you can set the sensor location on the map. Click on "Set Sensor" and then "Set Position" buttons allowing you to then use the cursor and click on the map where you've placed the radar module. Once the position is set, the application will allow you to rotate the heading of the module using the slider that appears. Confirm the heading angle and click "Set Heading."

### 3. Configure Motion Trail

The visualized target marker can have a "trail" added via the configuration. Enabling the trail and setting the trail duration will illuminate the marker's trail with the darkest opacity at the current location, fading to transparent, based on the "Trail Duration" setting. Also available is "Target Smoothing" which can be set in 10-steps from Low to High, which will average the data frame position and velocity data against previous frames, helping to reduce jitter in the data output from the module.

### 4. Observe Targets

Targets appear on the SVG map in real-time, color-coded by velocity.  
Trails fade based on `trail_duration`. Audible alerts indicate proximity.

### 5. Logging

Each run generates a log directory under `./logs/YYYY-MM-DD/`.  
Within this folder:
- `YYYY-MM-DD_TrackIndex.csv` — metadata of all tracked targets. (first_seen_iso, serial, last_seen_iso, duration, verbose_file)
- `Txx_HHMMSS.csv` — per-target detailed CSV logs (timestamp, x_mm, y_mm, range_mm, speed_mm_s, accel_mm_s2, raw_hex).

---

## Playback Mode

Tracked target playback is available via the main menu where you can load previously logged CSV's of the target data frames.

Playback Features:
- Load any `Txx_*.csv` log via drag-and-drop or via the native file browser
- Playback speed from 1× to 20×
- Toggle trails and smoothing
- Visual HUD showing elapsed time and sensor orientation

---

## Architecture Summary

- `main.py` — Launches and initializes configuration and GUI.  
- `radar/gui.py` — Core visualization window; manages rendering, sensor setup, and live updates.  
- `radar/mqtt_client.py` — Handles inbound MQTT data and frame parsing.  
- `radar/serial_reader.py` — Non-blocking serial input with frame parsing and callback dispatch.  
- `radar/tracking.py` — Target management and log persistence.  
- `radar/playback_gui.py` — Replay previously logged tracks.  
- `radar/svg_utils.py` — Rasterizes SVGs using CairoSVG for accurate map scaling.  
- `radar/sound.py` — Manages distance and speed-based audible feedback.  
- `radar/config.py` — JSON configuration management.  
- `radar/constants.py` — Centralized constants for color, font, and path references.

---

## Example Workflow

1. Connect radar module via USB or set up MQTT publisher  
2. Launch `python3 main.py`  
3. Use GUI configuration to select mode and confirm connection
4. Observe targets in real time; CSV logs will be written automatically
5. Run playback viewer to replay or analyze motion patterns

---

## Development Notes

- PondEyes was designed in an effort to research the effectiveness of mm-wave radar modules for tracking human targets in a private space
- Code modularity allows replacement of radar backends or visualization layers
- `radar/tracking.py` uses per-day indexing for persistence and log integrity
- Logging system creates one CSV per tracked target and a per-day index
- Uses CairoSVG for vector scaling; SVG maps should define physical size (in mm or cm) for accurate projection

---

Copyright © 2025 David D. Karnowski.

---
