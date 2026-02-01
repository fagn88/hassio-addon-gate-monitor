#!/usr/bin/env python3
"""Gate Monitor - Detect gate status using Gemini Vision API."""

import json
import sys
import time
from pathlib import Path

import cv2
import paho.mqtt.client as mqtt
from google import genai
from google.genai import types
from PIL import Image
import io

# Enable unbuffered output for real-time logging
sys.stdout.reconfigure(line_buffering=True)

CONFIG_PATH = Path("/data/options.json")

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


def analyze_gate(client: genai.Client, image_data: bytes) -> str:
    """Send image to Gemini Vision and get gate status."""
    log("vision", "Analyzing image with Gemini Vision...")

    try:
        # Convert bytes to PIL Image for Gemini
        image = Image.open(io.BytesIO(image_data))

        response = client.models.generate_content(
            model="gemini-1.5-pro",
            contents=[VISION_PROMPT, image],
        )

        result = response.text.strip().upper()
        log("vision", f"Gemini response: {result}")

        if result in ("OPEN", "CLOSED", "UNKNOWN"):
            return result.lower()

        log("vision", "Unexpected response, treating as unknown")
        return "unknown"

    except Exception as e:
        log("vision", f"ERROR: API call failed: {e}")
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


def publish_alert(client: mqtt.Client, topic_prefix: str, camera_name: str) -> None:
    """Publish gate open alert to MQTT."""
    topic = f"{topic_prefix}/{camera_name}/alert"
    client.publish(topic, "gate_open")
    log("mqtt", f"Published alert to {topic}")


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
    log("main", "Gemini Vision initialized")

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
                status = analyze_gate(gemini_client, image_data)

                if status != "error":
                    # Publish status
                    publish_status(mqtt_client, topic_prefix, camera_name, status)

                    # Send alert if gate is open
                    if status == "open":
                        publish_alert(mqtt_client, topic_prefix, camera_name)
                        log("main", "GATE IS OPEN - Alert sent")
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
