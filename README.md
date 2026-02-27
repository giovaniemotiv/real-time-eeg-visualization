# Emotiv Real-Time EEG Visualization

This project is a Python-based real-time EEG data visualization tool. It connects directly to the **Emotiv Cortex API (v2)** using WebSockets to stream live brainwave data from connected Emotiv headsets (like EPOC X or Insight) and plots the data immediately using high-performance graphics.

## Features

*   **Direct Cortex API Integration:** Handles authentication, headset discovery, session creation, and data subscription automatically.
*   **Real-time Streaming:** Subscribes to the live `eeg` data stream via WebSockets.
*   **High-Performance Plotting:** Utilizes `pyqtgraph` to render multiple channels of high-frequency data smoothly at 30 FPS.
*   **Multithreaded Architecture:** Runs the WebSocket communication and data gathering in a background thread to ensure the UI remains responsive.
*   **Customizable:** Easily change the channels to monitor, the size of the time window, or vertical spacing of the plots.

## Prerequisites

To run this application, you must have the following installed:

1.  **Python 3.7+**
2.  **Emotiv Launcher (Cortex Service)**: This must be running in the background. Either connect a physical headset or start a virtual headset from within the Emotiv Launcher.
3.  **Cortex API Credentials**: You need a `CLIENT_ID` and `CLIENT_SECRET` generated from your Emotiv account.

### Required Python Packages

Install the necessary dependencies using `pip`:

```bash
pip install numpy websocket-client pyqtgraph PyQt5
```
*(Note: `pyqtgraph` requires a Qt binding like `PyQt5`, `PyQt6`, or `PySide6` to be installed.)*

## Setup & Configuration

Before running the script, open `emotiv_realtime_plot.py` and ensure the configuration block at the top matches your environment:

1.  **API Credentials**:
    Update the `CLIENT_ID` and `CLIENT_SECRET` with your own keys.
    ```python
    CLIENT_ID = "your_client_id_here"
    CLIENT_SECRET = "your_client_secret_here"
    ```

2.  **Root CA Certificate**:
    If you are using `wss://localhost:6868`, Cortex requires a valid SSL certificate. Ensure `rootCA.pem` is present in the same directory as the script, or adjust `ROOT_CA_PATH`. If you prefer to bypass SSL verification (not recommended for production), set `USE_ROOT_CA = False`.

3.  **Channel Layout**:
    Adjust the `CHANNEL_NAMES` and `CHANNEL_START_INDEX` to match your specific headset's data layout. The default is configured for specific channels (AF3, T7, Pz, T8, AF4).

## How to Run

1.  Ensure **Emotiv Launcher** is open and your headset is turned on and connected (or a virtual headset is active).
2.  Run the script from your terminal:

```bash
python emotiv_realtime_plot.py
```

3.  The application will automatically attempt to authorize, connect to the first available headset, create an active session, and begin rendering the live EEG data streams on the screen.

## Project Architecture

The codebase has been refactored into three primary components for maintainability:

1.  **`CortexSocketClient`**: A low-level wrapper around `websocket-client` that handles the JSON-RPC 2.0 protocol mechanics, tracking Request IDs, timeouts, and buffering incoming asynchronous events (like EEG packets).
2.  **`CortexSessionManager`**: Runs in a background daemon thread. Handles the complex Cortex API state machine (Request Access -> Authorize -> Query Headsets -> Connect -> Create Session -> Subscribe).
3.  **`RealtimeEEGPlotter`**: The main UI application class. It sets up the PyQtGraph windows, contains the main rendering loop driven by a `QTimer`, and safely pulls data across the thread boundary from the `CortexSessionManager`.

## Troubleshooting
*   **Connection Refused:** Ensure the Cortex service is running (usually started alongside the Emotiv Launcher) and reachable at `wss://localhost:6868`.
*   **No Headsets Found:** Make sure a headset is turned on, paired, and visible in the Emotiv Launcher, or start a virtual headset.
*   **Token / EULA Errors:** The first time you use a new Client ID, Cortex requires you to approve the application in the Emotiv Launcher UI. Open the Launcher and look for a notification asking to approve access.
