"""
Emotiv Real-Time EEG Visualization

This module connects to the Emotiv Cortex API via WebSockets to stream and 
visualize real-time EEG data using PyQtGraph. It handles authentication,
session management, data subscription, and signal rendering.
"""

import json
import os
import threading
import ssl
from collections import deque
from typing import List, Optional, Dict, Any

import numpy as np
import websocket

import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

# ==========================================
# CONFIGURATION
# ==========================================
CORTEX_URL = "wss://localhost:6868"
CLIENT_ID = os.getenv("CORTEX_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CORTEX_CLIENT_SECRET", "")

USE_ROOT_CA = True
ROOT_CA_PATH = "rootCA.pem"

# EEG Layout and Channel Settings
CHANNEL_NAMES = ["AF3", "T7", "Pz", "T8", "AF4"]
CHANNEL_START_INDEX = 2
CHANNEL_COUNT = len(CHANNEL_NAMES)

# Plotting Configuration
SECONDS_VISIBLE = 5
UI_FPS = 30
SAMPLING_RATE_FALLBACK = 128
CHANNEL_OFFSET = 200.0


# ==========================================
# TIMING UTILITIES
# ==========================================
_global_timer = QtCore.QElapsedTimer()
_global_timer.start()

def get_time_monotonic() -> float:
    """Return monotonically increasing time in seconds."""
    return _global_timer.nsecsElapsed() / 1e9

def sleep_ms(milliseconds: int) -> None:
    """Pause execution for the specified milliseconds."""
    QtCore.QThread.msleep(milliseconds)


# ==========================================
# CORTEX API CLIENT
# ==========================================
class CortexSocketClient:
    """Handles raw WebSocket communication and RPC messaging with Cortex."""
    
    def __init__(self, websocket_connection: websocket.WebSocket):
        self._ws = websocket_connection
        
        # Request ID tracking
        self._current_request_id = 1
        self._id_lock = threading.Lock()

        # Responses and warnings storage
        self._response_lock = threading.Lock()
        self._responses_by_id: Dict[int, Any] = {}

        self._warning_lock = threading.Lock()
        self._warnings: deque = deque()

        # Data stream storage
        self._stream_lock = threading.Lock()
        self._eeg_messages: deque = deque()

        # Background thread for listening to incoming messages
        self._stop_requested = False
        self._listener_thread = threading.Thread(target=self._listen_for_messages, daemon=True)
        self._listener_thread.start()

    def close(self):
        """Close the WebSocket connection and stop the listener thread."""
        self._stop_requested = True
        try:
            self._ws.close()
        except Exception:
            pass

    def _generate_request_id(self) -> int:
        """Generate a unique incrementing request ID in a thread-safe manner."""
        with self._id_lock:
            request_id = self._current_request_id
            self._current_request_id += 1
            return request_id

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None, request_id: Optional[int] = None) -> int:
        """Send a JSON-RPC request to the Cortex API."""
        if request_id is None:
            request_id = self._generate_request_id()
            
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
            
        self._ws.send(json.dumps(payload))
        return request_id

    def wait_for_response(self, request_id: int, timeout_seconds: int = 30) -> Any:
        """Block until a response for a specific request ID is received."""
        deadline = get_time_monotonic() + float(timeout_seconds)
        
        while get_time_monotonic() < deadline:
            with self._response_lock:
                if request_id in self._responses_by_id:
                    message = self._responses_by_id.pop(request_id)
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result")
            sleep_ms(10)
            
        raise TimeoutError(f"Timeout waiting for response to request ID: {request_id}")

    def wait_for_warning(self, warning_code: int, timeout_seconds: int = 30) -> Dict[str, Any]:
        """Block until a specific warning code is received from the API."""
        deadline = get_time_monotonic() + float(timeout_seconds)
        
        while get_time_monotonic() < deadline:
            with self._warning_lock:
                for warning in list(self._warnings):
                    if warning.get("code") == warning_code:
                        self._warnings.remove(warning)
                        return warning
            sleep_ms(10)
            
        raise TimeoutError(f"Timeout waiting for warning code: {warning_code}")

    def extract_eeg_messages(self, max_messages: int = 2000) -> List[Dict[str, Any]]:
        """Retrieve and remove up to max_messages EEG packets from the buffer."""
        messages = []
        with self._stream_lock:
            while self._eeg_messages and len(messages) < max_messages:
                messages.append(self._eeg_messages.popleft())
        return messages

    def _listen_for_messages(self):
        """Continuously listen for incoming WebSocket messages."""
        while not self._stop_requested:
            try:
                raw_data = self._ws.recv()
                if not raw_data:
                    continue
                message = json.loads(raw_data)
            except Exception:
                break

            # Process JSON-RPC responses
            if "id" in message and ("result" in message or "error" in message):
                with self._response_lock:
                    self._responses_by_id[message["id"]] = message
                continue

            # Process warning messages
            if "warning" in message:
                with self._warning_lock:
                    self._warnings.append(message["warning"])
                continue

            # Process EEG data streams
            if "eeg" in message:
                with self._stream_lock:
                    self._eeg_messages.append(message)
                continue


# ==========================================
# CORTEX SESSION MANAGER
# ==========================================
class CortexSessionManager(threading.Thread):
    """Manages the full lifecycle of a Cortex session in a separate thread."""
    
    def __init__(self, shared_state: Dict[str, Any]):
        super().__init__(daemon=True)
        self.shared_state = shared_state
        self.stop_requested = False
        self.cortex_client: Optional[CortexSocketClient] = None
        self.auth_token: Optional[str] = None
        self.session_id: Optional[str] = None

    def create_connection(self) -> CortexSocketClient:
        """Establish the secure WebSocket connection."""
        ssl_options = {"cert_reqs": ssl.CERT_NONE}
        if USE_ROOT_CA:
            ssl_options = {"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": ROOT_CA_PATH}

        ws = websocket.create_connection(CORTEX_URL, sslopt=ssl_options, timeout=30)
        return CortexSocketClient(ws)

    def authorize(self) -> str:
        """Authenticate with the Cortex API and obtain a token."""
        # Request access
        req_id = self.cortex_client.send_request("requestAccess", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        self.cortex_client.wait_for_response(req_id, timeout_seconds=30)

        # Authorize and get token
        req_id = self.cortex_client.send_request("authorize", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        auth_response = self.cortex_client.wait_for_response(req_id, timeout_seconds=30) or {}
        
        token = auth_response.get("cortexToken")
        if not token:
            raise RuntimeError("Authorization failed: No cortexToken returned.")
        return token

    def connect_headset(self) -> str:
        """Find, connect to, and return the ID of an available headset."""
        # Refresh device list
        req_id = self.cortex_client.send_request("controlDevice", {"command": "refresh"})
        self.cortex_client.wait_for_response(req_id, timeout_seconds=30)
        self.cortex_client.wait_for_warning(142, timeout_seconds=60)

        # Query discovered headsets
        req_id = self.cortex_client.send_request("queryHeadsets", {})
        headsets = self.cortex_client.wait_for_response(req_id, timeout_seconds=30) or []
        
        if not headsets:
            raise RuntimeError("No headsets found. Please connect one or start a virtual headset.")
            
        headset = headsets[0]
        headset_id = headset.get("id")
        status = headset.get("status")

        # Connect if not already connected
        if status != "connected":
            req_id = self.cortex_client.send_request("controlDevice", {"command": "connect", "headset": headset_id})
            self.cortex_client.wait_for_response(req_id, timeout_seconds=30)
            self.cortex_client.wait_for_warning(104, timeout_seconds=60)
            
        return headset_id

    def setup_session(self, token: str, headset_id: str) -> str:
        """Create an active session for the specified headset."""
        req_id = self.cortex_client.send_request("createSession", {
            "cortexToken": token, 
            "headset": headset_id, 
            "status": "active"
        })
        session_response = self.cortex_client.wait_for_response(req_id, timeout_seconds=30) or {}
        
        session_id = session_response.get("id")
        if not session_id:
            raise RuntimeError("Session creation failed: No session ID returned.")
        return session_id

    def run(self):
        """Main thread execution: connect, authenticate, setup session, and subscribe."""
        try:
            self.cortex_client = self.create_connection()
            
            # Step 1: Connect and Authorize
            headset_id = self.connect_headset()
            self.auth_token = self.authorize()
            
            # Step 2: Create Session
            self.session_id = self.setup_session(self.auth_token, headset_id)
            
            # Step 3: Subscribe to EEG data
            req_id = self.cortex_client.send_request("subscribe", {
                "cortexToken": self.auth_token, 
                "session": self.session_id, 
                "streams": ["eeg"]
            })
            self.cortex_client.wait_for_response(req_id, timeout_seconds=30)

            # Update shared state to indicate readiness
            with self.shared_state["lock"]:
                self.shared_state["cortex_client"] = self.cortex_client
                self.shared_state["ready"] = True

            # Keep thread alive until requested to stop
            while not self.stop_requested:
                sleep_ms(50)

        except Exception as error:
            with self.shared_state["lock"]:
                self.shared_state["error"] = str(error)

        finally:
            self.cleanup()

    def cleanup(self):
        """Close session and connection gracefully."""
        try:
            if self.auth_token and self.session_id and self.cortex_client:
                req_id = self.cortex_client.send_request("updateSession", {
                    "cortexToken": self.auth_token, 
                    "session": self.session_id, 
                    "status": "close"
                })
                self.cortex_client.wait_for_response(req_id, timeout_seconds=10)
        except Exception:
            pass
            
        if self.cortex_client:
            self.cortex_client.close()


# ==========================================
# EEG DATA PROCESSING
# ==========================================
def extract_eeg_channels(message: Dict[str, Any]) -> Optional[List[float]]:
    """
    Extract relevant EEG channels from a raw cortex API message based on configuration.
    
    Expected format: {"eeg": [count, timestamp, val1, val2, ..., marker, ...]}
    """
    if not isinstance(message, dict):
        return None
        
    eeg_data = message.get("eeg")
    if not isinstance(eeg_data, list):
        return None
        
    required_length = CHANNEL_START_INDEX + CHANNEL_COUNT
    if len(eeg_data) < required_length:
        return None

    # Slice out the requested channels
    channel_values = eeg_data[CHANNEL_START_INDEX:required_length]
    
    extracted_values = []
    for value in channel_values:
        try:
            extracted_values.append(float(value))
        except (ValueError, TypeError):
            return None
            
    return extracted_values


# ==========================================
# UI & PLOTTING CONTROLLER
# ==========================================
class RealtimeEEGPlotter:
    """Manages the PyQtGraph interface and renders incoming EEG streams."""
    
    def __init__(self):
        self.app = QtWidgets.QApplication([])
        
        # Shared state for communication with the worker thread
        self.shared_state = {
            "lock": threading.Lock(),
            "ready": False,
            "error": None,
            "cortex_client": None,
        }
        
        # Start connection manager
        self.session_manager = CortexSessionManager(self.shared_state)
        self.session_manager.start()

        # Initialize UI components
        self.setup_ui()
        self.setup_timer()

    def setup_ui(self):
        """Create the window, labels, and plots."""
        self.window = pg.GraphicsLayoutWidget(title="EMOTIV EEG Realtime (Cortex)")
        self.window.resize(1200, 700)

        # Status label at the top
        self.status_label = pg.LabelItem(justify="left")
        self.window.addItem(self.status_label, row=0, col=0)

        # Main Plot
        self.plot_area = self.window.addPlot(row=1, col=0)
        self.plot_area.setLabel("left", "EEG (raw units)")
        self.plot_area.setLabel("bottom", "Samples")
        self.plot_area.showGrid(x=True, y=True)
        self.plot_area.addLegend()

        # Plot Data Variables
        self.curves: List[pg.PlotDataItem] = []
        self.data_buffer: Optional[np.ndarray] = None
        self.visible_samples = int(SECONDS_VISIBLE * SAMPLING_RATE_FALLBACK)
        self.total_packets_received = 0

    def setup_timer(self):
        """Configure the refresh timer for rendering."""
        self.update_timer = QtCore.QTimer()
        self.update_timer.timeout.connect(self.update_plot)
        self.update_timer.start(int(1000 / UI_FPS))
        
        # Ensure cleanup on quit
        self.app.aboutToQuit.connect(self.on_application_quit)

    def process_incoming_messages(self, cortex_client: CortexSocketClient) -> List[List[float]]:
        """Fetch and parse new EEG messages from the client."""
        raw_messages = cortex_client.extract_eeg_messages(max_messages=500)
        
        if not raw_messages:
            self.status_label.setText("Streaming EEG… but 0 packets received.")
            return []

        self.total_packets_received += len(raw_messages)
        parsed_rows = []
        
        for msg in raw_messages:
            row = extract_eeg_channels(msg)
            if row is not None:
                parsed_rows.append(row)
                
        self.status_label.setText(
            f"EEG packets: {self.total_packets_received} | "
            f"msgs this frame: {len(raw_messages)} | "
            f"usable rows: {len(parsed_rows)}"
        )
        
        return parsed_rows

    def update_plot(self):
        """Main rendering loop: fetches data, shifts buffer, redraws curves."""
        with self.shared_state["lock"]:
            error = self.shared_state["error"]
            is_ready = self.shared_state["ready"]
            cortex_client = self.shared_state["cortex_client"]

        # Handle pre-connection states
        if error:
            self.status_label.setText(f"Connection Error: {error}")
            return

        if not is_ready or cortex_client is None:
            self.status_label.setText("Connecting to Cortex API…")
            return

        # Fetch and process data
        new_data_rows = self.process_incoming_messages(cortex_client)
        if not new_data_rows:
            return

        # Initialize plot curves and buffer on first data batch
        if self.data_buffer is None:
            self.data_buffer = np.zeros((CHANNEL_COUNT, self.visible_samples), dtype=np.float32)
            self.plot_area.clear()
            self.plot_area.addLegend()
            
            for channel_name in CHANNEL_NAMES:
                self.curves.append(self.plot_area.plot(name=channel_name))
            self.plot_area.enableAutoRange(True, True)

        # Transpose to (channels, samples) shape
        incoming_block = np.asarray(new_data_rows, dtype=np.float32).T
        num_new_samples = incoming_block.shape[1]

        # Shift ring buffer and append new data
        if num_new_samples >= self.data_buffer.shape[1]:
            # Keep only the newest portion if block is huge
            self.data_buffer[:] = incoming_block[:, -self.data_buffer.shape[1]:]
        else:
            # Shift old data left, add new data to the right
            self.data_buffer[:, :-num_new_samples] = self.data_buffer[:, num_new_samples:]
            self.data_buffer[:, -num_new_samples:] = incoming_block

        # Render curves with horizontal offsets
        for index, curve in enumerate(self.curves):
            offset_data = self.data_buffer[index] + (index * CHANNEL_OFFSET)
            curve.setData(offset_data)

    def on_application_quit(self):
        """Signal the background thread to shut down cleanly."""
        self.session_manager.stop_requested = True

    def run(self):
        """Show window and block on PyQt event loop."""
        self.window.show()
        self.app.exec_()


# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    plotter = RealtimeEEGPlotter()
    plotter.run()