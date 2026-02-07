#!/usr/bin/env python3
"""Gate Monitor - Detect gate status using Gemini Vision API."""

import json
import re
import sys
import time
from datetime import datetime
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
SNAPSHOT_DIR = Path("/config/www/gate-monitor")
REFERENCE_DIR = Path("/config/www/gate-monitor/reference")

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

VISION_PROMPT_WITH_REFS = """You are a gate status classifier. Your task is to determine if a gate is OPEN or CLOSED by comparing the query image against the reference examples provided.

INSTRUCTIONS:
- Compare the query image carefully against the labeled reference images
- CLOSED: The gate is upright and aligned with the fence/wall, bars are vertical
- OPEN: The gate is rotated/swung inward, creating an angle or gap
- If unsure, respond UNKNOWN with low confidence

Respond ONLY with valid JSON (no markdown, no extra text):
{"status": "OPEN", "confidence": 85}
{"status": "CLOSED", "confidence": 95}
{"status": "UNKNOWN", "confidence": 30}"""

VISION_PROMPT_NO_REFS = """You are a gate status classifier analyzing a cropped image of a metal bar gate.

INSTRUCTIONS:
- CLOSED: The gate is upright, vertical bars aligned with the fence/wall
- OPEN: The gate is rotated/swung inward, creating an angle or visible gap
- If unsure, respond UNKNOWN with low confidence

Respond ONLY with valid JSON (no markdown, no extra text):
{"status": "OPEN", "confidence": 85}
{"status": "CLOSED", "confidence": 95}
{"status": "UNKNOWN", "confidence": 30}"""

# Gate region crop coordinates (percentage of frame)
# Based on camera view: gate is in upper-left corner
GATE_CROP = {
    "x_start": 0.0,    # 0% from left
    "x_end": 0.25,     # 25% from left
    "y_start": 0.10,   # 10% from top
    "y_end": 0.55,     # 55% from top
}

# Reference image filenames to look for
REFERENCE_FILENAMES = [
    "closed_day.jpg",
    "closed_night.jpg",
    "open_day.jpg",
    "open_night.jpg",
]


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


def load_reference_images() -> list:
    """Load reference images from the reference directory.

    Returns a list of (label, PIL.Image) tuples for few-shot prompting.
    """
    references = []

    if not REFERENCE_DIR.exists():
        log("reference", f"Reference directory not found: {REFERENCE_DIR}")
        log("reference", "Running in zero-shot mode (no reference images)")
        return references

    label_map = {
        "closed_day.jpg": "Example - CLOSED gate (daytime):",
        "closed_night.jpg": "Example - CLOSED gate (nighttime):",
        "open_day.jpg": "Example - OPEN gate (daytime):",
        "open_night.jpg": "Example - OPEN gate (nighttime):",
    }

    for filename in REFERENCE_FILENAMES:
        filepath = REFERENCE_DIR / filename
        if filepath.exists():
            try:
                img = Image.open(filepath)
                img.load()  # Force load into memory
                label = label_map[filename]
                references.append((label, img))
                log("reference", f"Loaded: {filename}")
            except Exception as e:
                log("reference", f"ERROR loading {filename}: {e}")
        else:
            log("reference", f"Not found (optional): {filename}")

    if references:
        log("reference", f"Loaded {len(references)} reference images for few-shot mode")
    else:
        log("reference", "No reference images found, running in zero-shot mode")

    return references


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


def capture_rtsp_frame(rtsp_url: str) -> tuple[bytes, bytes] | tuple[None, None]:
    """Capture a single frame from RTSP stream.

    Returns:
        Tuple of (full_frame_bytes, cropped_gate_bytes) or (None, None) on error
    """
    log("camera", "Capturing frame from camera...")

    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        log("camera", "ERROR: Failed to open RTSP stream")
        return None, None

    ret, frame = cap.read()
    cap.release()

    if not ret:
        log("camera", "ERROR: Failed to capture frame")
        return None, None

    # Resize full frame
    frame = cv2.resize(frame, (640, 480))
    height, width = frame.shape[:2]

    # Encode full frame for snapshot
    _, full_buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

    # Crop to gate region for analysis
    x1 = int(width * GATE_CROP["x_start"])
    x2 = int(width * GATE_CROP["x_end"])
    y1 = int(height * GATE_CROP["y_start"])
    y2 = int(height * GATE_CROP["y_end"])

    cropped = frame[y1:y2, x1:x2]
    _, crop_buffer = cv2.imencode('.jpg', cropped, [cv2.IMWRITE_JPEG_QUALITY, 90])

    log("camera", f"Frame captured: full 640x480, gate crop {x2-x1}x{y2-y1}")
    return full_buffer.tobytes(), crop_buffer.tobytes()


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


def parse_gate_response(response_text: str) -> tuple[str, int]:
    """Parse Gemini response to extract status and confidence.

    Returns:
        Tuple of (status, confidence) where status is 'open'/'closed'/'unknown'
        and confidence is 0-100.
    """
    text = response_text.strip()

    # Try parsing as JSON first
    try:
        # Strip markdown code fences if present
        cleaned = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
        cleaned = re.sub(r'\s*```$', '', cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()

        data = json.loads(cleaned)
        status = data.get("status", "UNKNOWN").upper()
        confidence = int(data.get("confidence", 0))

        if status in ("OPEN", "CLOSED", "UNKNOWN"):
            return status.lower(), min(max(confidence, 0), 100)
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    # Fallback: look for JSON-like pattern in the text
    json_match = re.search(r'\{[^}]*"status"\s*:\s*"(OPEN|CLOSED|UNKNOWN)"[^}]*\}', text, re.IGNORECASE)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            status = data.get("status", "UNKNOWN").upper()
            confidence = int(data.get("confidence", 0))
            return status.lower(), min(max(confidence, 0), 100)
        except (json.JSONDecodeError, ValueError):
            pass

    # Last resort: look for status keywords
    upper = text.upper()
    if "OPEN" in upper:
        return "open", 50  # Low confidence for unstructured response
    if "CLOSED" in upper:
        return "closed", 50
    return "unknown", 0


def build_contents(reference_images: list, query_image: Image.Image) -> list:
    """Build the contents list for the Gemini API call.

    Args:
        reference_images: List of (label, PIL.Image) tuples
        query_image: The current gate image to classify
    """
    contents = []

    if reference_images:
        contents.append(VISION_PROMPT_WITH_REFS)
        # Add reference images as few-shot examples
        for label, img in reference_images:
            contents.append(label)
            contents.append(img)
        contents.append("Now classify this image:")
    else:
        contents.append(VISION_PROMPT_NO_REFS)

    contents.append(query_image)
    return contents


def analyze_gate(
    client: genai.Client,
    model_name: str,
    image_data: bytes,
    reference_images: list,
    confidence_threshold: int,
    max_retries: int = 3,
) -> tuple[str, int]:
    """Send image to Gemini Vision and get gate status.

    Returns:
        Tuple of (status, confidence) where status is 'open'/'closed'/'unknown'/'error'
    """
    log("vision", f"Analyzing image with {model_name}...")

    # Convert bytes to PIL Image for Gemini
    query_image = Image.open(io.BytesIO(image_data))

    # Build contents with reference images
    contents = build_contents(reference_images, query_image)

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0.0),
            )

            raw = response.text.strip()
            log("vision", f"Gemini response: {raw}")

            status, confidence = parse_gate_response(raw)
            log("vision", f"Parsed: status={status}, confidence={confidence}")

            if confidence < confidence_threshold:
                log("vision", f"Confidence {confidence} below threshold {confidence_threshold}, treating as unknown")
                return "unknown", confidence

            return status, confidence

        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                # Rate limited - wait and retry
                wait_time = 60 * (attempt + 1)  # 60s, 120s, 180s
                log("vision", f"Rate limited. Waiting {wait_time}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
            else:
                log("vision", f"ERROR: API call failed: {e}")
                return "error", 0

    log("vision", "ERROR: Max retries exceeded due to rate limiting")
    return "error", 0


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
    confidence_threshold = config.get("confidence_threshold", 70)

    log("main", f"Camera: {camera_name}")
    log("main", f"Check interval: {check_interval // 60} minutes")
    log("main", f"Confidence threshold: {confidence_threshold}%")

    # Initialize Gemini client
    gemini_client = genai.Client(api_key=api_key)

    # Find the best available model
    model_name = find_best_model(gemini_client)
    if not model_name:
        log("main", "ERROR: Could not find a suitable Gemini model")
        sys.exit(1)

    log("main", f"Using model: {model_name}")

    # Load reference images for few-shot prompting
    reference_images = load_reference_images()

    # Initialize MQTT
    mqtt_client = create_mqtt_client(config)

    # Publish online status
    publish_addon_status(mqtt_client, topic_prefix, "online")

    # Initial delay to allow services to stabilize
    time.sleep(5)

    try:
        while True:
            log("main", "Starting gate check...")

            # Capture frame (full for snapshot, cropped for analysis)
            full_frame, gate_crop = capture_rtsp_frame(rtsp_url)

            if full_frame and gate_crop:
                # Analyze cropped gate region with Gemini Vision
                status, confidence = analyze_gate(
                    gemini_client, model_name, gate_crop,
                    reference_images, confidence_threshold,
                )

                if status != "error":
                    # If gate detected as OPEN, do immediate confirmation (best of 3)
                    if status == "open":
                        log("main", f"[1/3] Gate appears OPEN (confidence: {confidence}%). Confirming immediately...")

                        # 2nd check - immediate
                        full_frame_2, gate_crop_2 = capture_rtsp_frame(rtsp_url)
                        if full_frame_2 and gate_crop_2:
                            status_2, confidence_2 = analyze_gate(
                                gemini_client, model_name, gate_crop_2,
                                reference_images, confidence_threshold,
                            )
                            if status_2 == "open":
                                # Both agree: OPEN confirmed
                                log("main", f"[2/3] Confirmed OPEN (confidence: {confidence_2}%). Gate is open.")
                                status = "open"
                                full_frame = full_frame_2
                            else:
                                # Disagreement (OPEN vs CLOSED/UNKNOWN) - tiebreaker
                                log("main", f"[2/3] Got {status_2} (confidence: {confidence_2}%). Disagreement, doing tiebreaker...")
                                full_frame_3, gate_crop_3 = capture_rtsp_frame(rtsp_url)
                                if full_frame_3 and gate_crop_3:
                                    status_3, confidence_3 = analyze_gate(
                                        gemini_client, model_name, gate_crop_3,
                                        reference_images, confidence_threshold,
                                    )
                                    log("main", f"[3/3] Tiebreaker: {status_3} (confidence: {confidence_3}%)")
                                    status = status_3
                                    confidence = confidence_3
                                    full_frame = full_frame_3
                                else:
                                    log("main", "[3/3] Tiebreaker capture failed, using 2nd result (closed)")
                                    status = status_2
                                    confidence = confidence_2
                                    full_frame = full_frame_2
                        else:
                            log("main", "[2/3] Confirmation capture failed, discarding OPEN detection")
                            status = "unknown"

                    # Publish status
                    publish_status(mqtt_client, topic_prefix, camera_name, status)

                    # Send alert if gate is confirmed open
                    if status == "open":
                        snapshot_path = save_snapshot(full_frame, camera_name)
                        publish_alert(mqtt_client, topic_prefix, camera_name, snapshot_path)
                        log("main", "GATE IS OPEN - Alert sent with snapshot")
                    else:
                        log("main", f"Gate status: {status} (confidence: {confidence}%)")
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
