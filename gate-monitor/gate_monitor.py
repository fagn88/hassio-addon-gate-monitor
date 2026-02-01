#!/usr/bin/env python3
"""Gate Monitor - Detect gate status using Gemini Vision API."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import paho.mqtt.client as mqtt
from google import genai
from PIL import Image
import io

# Enable unbuffered output for real-time logging
sys.stdout.reconfigure(line_buffering=True)

CONFIG_PATH = Path("/data/options.json")
SNAPSHOT_DIR = Path("/config/www/gate-monitor")

# Preferred models in order (flash models first - higher free tier limits)
PREFERRED_MODELS = [
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-3-pro",
    "gemini-2.5-pro",
    "gemini-2.0-pro",
    "gemini-1.5-pro",
    "gemini-pro",
]

VISION_PROMPT = """Analisa esta imagem de uma câmara de segurança exterior.
O portão da rua está visível na imagem.
Responde APENAS com uma palavra:
- "OPEN" se o portão estiver aberto (consegues ver através dele ou está afastado da posição fechada)
- "CLOSED" se o portão estiver fechado (na posição normal fechada)
- "UNKNOWN" se não conseguires determinar

Responde apenas com a palavra, sem explicação."""


def log(module: str, message: str) -> None:
    """Print a log message with module prefix."""
    print(f"[{module}] {message}")


def load_config() -> dict:
    """Load configuration from Home Assistant options."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    log("config", "Config file not found, using defaults")
    return {}


def find_best_model(client: genai.Client) -> str | None:
    """List available models and return the best one for vision tasks."""
    log("models", "Listing available Gemini models...")

    available_models = []
    try:
        for model in client.models.list():
            model_name = model.name
            # Strip "models/" prefix if present
            if model_name.startswith("models/"):
                model_name = model_name[7:]

            if "gemini" in model_name.lower():
                available_models.append(model_name)
                log("models", f"  Found: {model_name}")
    except Exception as e:
        log("models", f"ERROR listing models: {e}")
        return None

    # Find the best available model from our preference list
    for preferred in PREFERRED_MODELS:
        for available in available_models:
            # Check if the preferred model matches (with or without version suffix)
            if available.startswith(preferred) or available == preferred:
                log("models", f"Selected model: {available}")
                return available

    # If none of our preferred models found, try the first gemini model
    for available in available_models:
        if "gemini" in available.lower() and "pro" in available.lower():
            log("models", f"Fallback model: {available}")
            return available

    if available_models:
        log("models", f"Using first available: {available_models[0]}")
        return available_models[0]

    log("models", "ERROR: No Gemini models found!")
    return None


def capture_rtsp_frame(rtsp_url: str) -> bytes | None:
    """Capture a single frame from RTSP stream."""
    log("camera", "Capturing frame from camera...")

    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        log("camera", "ERROR: Failed to open RTSP stream")
        return None

    ret, frame = cap.read()
    cap.release()

    if not ret:
        log("camera", "ERROR: Failed to capture frame")
        return None

    # Resize to reduce tokens (640x480 is sufficient)
    frame = cv2.resize(frame, (640, 480))

    # Encode to JPEG
    _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    log("camera", "Frame captured successfully")
    return buffer.tobytes()


def save_snapshot(image_data: bytes, camera_name: str) -> str | None:
    """Save snapshot to /media/gate-monitor/ and return the path."""
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        # Save with timestamp for history
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = SNAPSHOT_DIR / f"{camera_name}_{timestamp}.jpg"

        with open(snapshot_path, "wb") as f:
            f.write(image_data)

        # Also save as "latest" for easy access
        latest_path = SNAPSHOT_DIR / f"{camera_name}_latest.jpg"
        with open(latest_path, "wb") as f:
            f.write(image_data)

        log("snapshot", f"Saved snapshot to {snapshot_path}")

        # Return /local/ path for HA notifications (maps to /config/www/)
        timestamp_url = int(datetime.now().timestamp())
        return f"/local/gate-monitor/{camera_name}_latest.jpg?v={timestamp_url}"

    except Exception as e:
        log("snapshot", f"ERROR saving snapshot: {e}")
        return None


def analyze_gate(client: genai.Client, model_name: str, image_data: bytes, max_retries: int = 3) -> str:
    """Send image to Gemini Vision and get gate status."""
    log("vision", f"Analyzing image with {model_name}...")

    # Convert bytes to PIL Image for Gemini
    image = Image.open(io.BytesIO(image_data))

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[VISION_PROMPT, image],
            )

            result = response.text.strip().upper()
            log("vision", f"Gemini response: {result}")

            if result in ("OPEN", "CLOSED", "UNKNOWN"):
                return result.lower()

            log("vision", "Unexpected response, treating as unknown")
            return "unknown"

        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                # Rate limited - wait and retry
                wait_time = 60 * (attempt + 1)  # 60s, 120s, 180s
                log("vision", f"Rate limited. Waiting {wait_time}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
            else:
                log("vision", f"ERROR: API call failed: {e}")
                return "error"

    log("vision", "ERROR: Max retries exceeded due to rate limiting")
    return "error"


def create_mqtt_client(config: dict) -> mqtt.Client:
    """Create and connect MQTT client."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    username = config.get("mqtt_username", "")
    password = config.get("mqtt_password", "")
    if username:
        client.username_pw_set(username, password)

    broker = config.get("mqtt_broker", "core-mosquitto")
    port = config.get("mqtt_port", 1883)

    log("mqtt", f"Connecting to MQTT broker {broker}:{port}...")

    try:
        client.connect(broker, port, 60)
        client.loop_start()
        log("mqtt", "Connected to MQTT broker")
    except Exception as e:
        log("mqtt", f"ERROR: Failed to connect: {e}")

    return client


def publish_status(client: mqtt.Client, topic_prefix: str, camera_name: str, status: str) -> None:
    """Publish gate status to MQTT."""
    topic = f"{topic_prefix}/{camera_name}/status"
    client.publish(topic, status, retain=True)
    log("mqtt", f"Published to {topic}: {status}")


def publish_alert(client: mqtt.Client, topic_prefix: str, camera_name: str, snapshot_path: str | None) -> None:
    """Publish gate open alert to MQTT with snapshot path."""
    topic = f"{topic_prefix}/{camera_name}/alert"

    # Send JSON payload with snapshot path
    payload = {
        "event": "gate_open",
        "camera": camera_name,
        "timestamp": datetime.now().isoformat(),
        "snapshot": snapshot_path,
    }

    client.publish(topic, json.dumps(payload))
    log("mqtt", f"Published alert to {topic} with snapshot: {snapshot_path}")


def publish_addon_status(client: mqtt.Client, topic_prefix: str, status: str) -> None:
    """Publish add-on online/offline status."""
    topic = f"{topic_prefix}/status"
    client.publish(topic, status, retain=True)
    log("mqtt", f"Add-on status: {status}")


def main() -> None:
    """Main entry point."""
    log("main", "Gate Monitor starting...")

    # Load configuration
    config = load_config()

    rtsp_url = config.get("rtsp_url", "")
    if not rtsp_url:
        log("main", "ERROR: rtsp_url not configured")
        sys.exit(1)

    api_key = config.get("gemini_api_key", "")
    if not api_key:
        log("main", "ERROR: gemini_api_key not configured")
        sys.exit(1)

    camera_name = config.get("camera_name", "exterior_frente")
    topic_prefix = config.get("mqtt_topic_prefix", "homeassistant/gate")
    check_interval = config.get("check_interval_minutes", 30) * 60

    log("main", f"Camera: {camera_name}")
    log("main", f"Check interval: {check_interval // 60} minutes")

    # Initialize Gemini client
    gemini_client = genai.Client(api_key=api_key)

    # Find the best available model
    model_name = find_best_model(gemini_client)
    if not model_name:
        log("main", "ERROR: Could not find a suitable Gemini model")
        sys.exit(1)

    log("main", f"Using model: {model_name}")

    # Initialize MQTT
    mqtt_client = create_mqtt_client(config)

    # Publish online status
    publish_addon_status(mqtt_client, topic_prefix, "online")

    # Initial delay to allow services to stabilize
    time.sleep(5)

    try:
        while True:
            log("main", "Starting gate check...")

            # Capture frame
            image_data = capture_rtsp_frame(rtsp_url)

            if image_data:
                # Analyze with Gemini Vision
                status = analyze_gate(gemini_client, model_name, image_data)

                if status != "error":
                    # Publish status
                    publish_status(mqtt_client, topic_prefix, camera_name, status)

                    # Send alert if gate is open
                    if status == "open":
                        # Save snapshot for notification
                        snapshot_path = save_snapshot(image_data, camera_name)
                        publish_alert(mqtt_client, topic_prefix, camera_name, snapshot_path)
                        log("main", "GATE IS OPEN - Alert sent with snapshot")
                    else:
                        log("main", f"Gate status: {status}")
            else:
                log("main", "Skipping analysis due to capture failure")

            log("main", f"Next check in {check_interval // 60} minutes")
            time.sleep(check_interval)

    except KeyboardInterrupt:
        log("main", "Shutting down...")
    finally:
        publish_addon_status(mqtt_client, topic_prefix, "offline")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        log("main", "Goodbye!")


if __name__ == "__main__":
    main()
