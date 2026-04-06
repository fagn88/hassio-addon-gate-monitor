#!/usr/bin/env python3
"""Gate Monitor - Detect gate status using Gemini Vision API."""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
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

# Model variants unsuitable for vision tasks
EXCLUDED_MODEL_SUFFIXES = ["-tts", "-lite", "-thinking", "-search"]

# Minimum Gemini model version to use (lower versions give unreliable results)
MIN_MODEL_VERSION = 3.0

# SSIM threshold for local comparison (0.0-1.0, higher = more similar required)
SSIM_THRESHOLD = 0.85

# Saturation threshold to distinguish day (color) from night (B&W/IR)
NIGHT_SATURATION_THRESHOLD = 30

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


def load_reference_images() -> dict:
    """Load reference images from the reference directory.

    Returns a dict with structure:
    {
        "pil": [(label, PIL.Image), ...],  -- for Gemini few-shot prompting
        "cv": {                             -- for local SSIM comparison
            "closed_day": np.ndarray | None,
            "closed_night": np.ndarray | None,
            "open_day": np.ndarray | None,
            "open_night": np.ndarray | None,
        }
    }
    """
    result = {
        "pil": [],
        "cv": {"closed_day": None, "closed_night": None, "open_day": None, "open_night": None},
    }

    if not REFERENCE_DIR.exists():
        log("reference", f"Reference directory not found: {REFERENCE_DIR}")
        log("reference", "Running in zero-shot mode (no reference images)")
        return result

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
                # Load PIL version for Gemini
                img = Image.open(filepath)
                img.load()
                w, h = img.size
                crop_box = (
                    int(w * GATE_CROP["x_start"]),
                    int(h * GATE_CROP["y_start"]),
                    int(w * GATE_CROP["x_end"]),
                    int(h * GATE_CROP["y_end"]),
                )
                img = img.crop(crop_box)
                label = label_map[filename]
                result["pil"].append((label, img))

                # Load OpenCV version for SSIM
                cv_img = cv2.imread(str(filepath))
                height, width = cv_img.shape[:2]
                x1 = int(width * GATE_CROP["x_start"])
                x2 = int(width * GATE_CROP["x_end"])
                y1 = int(height * GATE_CROP["y_start"])
                y2 = int(height * GATE_CROP["y_end"])
                cv_cropped = cv_img[y1:y2, x1:x2]
                cv_gray = cv2.cvtColor(cv_cropped, cv2.COLOR_BGR2GRAY)
                key = filename.replace(".jpg", "")
                result["cv"][key] = cv_gray
                log("reference", f"Loaded: {filename} (cropped to {img.size[0]}x{img.size[1]})")
            except Exception as e:
                log("reference", f"ERROR loading {filename}: {e}")
        else:
            log("reference", f"Not found (optional): {filename}")

    loaded_count = sum(1 for v in result["cv"].values() if v is not None)
    if loaded_count > 0:
        log("reference", f"Loaded {loaded_count} reference images (PIL + OpenCV)")
    else:
        log("reference", "No reference images found, running in zero-shot mode")

    return result


def is_night_frame(image_bgr: np.ndarray) -> bool:
    """Detect if a frame is night/IR (grayscale) based on color saturation."""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    mean_saturation = float(hsv[:, :, 1].mean())
    is_night = mean_saturation < NIGHT_SATURATION_THRESHOLD
    log("opencv", f"Mean saturation: {mean_saturation:.1f} ({'night' if is_night else 'day'})")
    return is_night


def compute_ssim(image_gray: np.ndarray, reference_gray: np.ndarray) -> float:
    """Compute SSIM between two grayscale images using OpenCV.

    Resizes reference to match image dimensions if needed.
    Returns similarity score 0.0-1.0.
    """
    if image_gray.shape != reference_gray.shape:
        reference_gray = cv2.resize(reference_gray, (image_gray.shape[1], image_gray.shape[0]))

    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2

    img1 = image_gray.astype(np.float64)
    img2 = reference_gray.astype(np.float64)

    mu1 = cv2.GaussianBlur(img1, (11, 11), 1.5)
    mu2 = cv2.GaussianBlur(img2, (11, 11), 1.5)

    mu1_sq = mu1 ** 2
    mu2_sq = mu2 ** 2
    mu1_mu2 = mu1 * mu2

    sigma1_sq = cv2.GaussianBlur(img1 ** 2, (11, 11), 1.5) - mu1_sq
    sigma2_sq = cv2.GaussianBlur(img2 ** 2, (11, 11), 1.5) - mu2_sq
    sigma12 = cv2.GaussianBlur(img1 * img2, (11, 11), 1.5) - mu1_mu2

    numerator = (2 * mu1_mu2 + c1) * (2 * sigma12 + c2)
    denominator = (mu1_sq + mu2_sq + c1) * (sigma1_sq + sigma2_sq + c2)

    ssim_map = numerator / denominator
    return float(ssim_map.mean())


def compare_local(gate_crop_bgr: np.ndarray, reference_images: dict, threshold: float = SSIM_THRESHOLD) -> tuple[str, float]:
    """Compare gate frame against reference images using SSIM.

    Args:
        gate_crop_bgr: Cropped gate region as BGR OpenCV array
        reference_images: The "cv" dict from load_reference_images()
        threshold: SSIM threshold for confident match

    Returns:
        Tuple of (status, best_ssim) where status is 'open'/'closed'/'inconclusive'
    """
    night = is_night_frame(gate_crop_bgr)
    suffix = "night" if night else "day"

    gate_gray = cv2.cvtColor(gate_crop_bgr, cv2.COLOR_BGR2GRAY)

    closed_ref = reference_images.get(f"closed_{suffix}")
    open_ref = reference_images.get(f"open_{suffix}")

    if closed_ref is None and open_ref is None:
        log("opencv", f"No {suffix} reference images available, skipping local comparison")
        return "inconclusive", 0.0

    scores = {}

    if closed_ref is not None:
        scores["closed"] = compute_ssim(gate_gray, closed_ref)
        log("opencv", f"SSIM closed_{suffix}: {scores['closed']:.3f}")

    if open_ref is not None:
        scores["open"] = compute_ssim(gate_gray, open_ref)
        log("opencv", f"SSIM open_{suffix}: {scores['open']:.3f}")

    # Prioritize closed: if closed is above threshold, skip API entirely
    if "closed" in scores and scores["closed"] >= threshold:
        log("opencv", f"Local match: closed (SSIM {scores['closed']:.3f} >= {threshold})")
        return "closed", scores["closed"]

    if "open" in scores and scores["open"] >= threshold:
        log("opencv", f"Local match: open (SSIM {scores['open']:.3f} >= {threshold})")
        return "open", scores["open"]

    best_status = max(scores, key=scores.get)
    log("opencv", f"Local inconclusive (best: {best_status} at {scores[best_status]:.3f} < {threshold})")
    return "inconclusive", scores[best_status]


def _parse_model_version(name: str) -> tuple[float, int, int]:
    """Extract sorting key from model name: (version, type_rank, stability_rank).

    Higher version = better. Flash preferred over Pro (lower type_rank).
    Stable preferred over preview (lower stability_rank).
    """
    # Extract version number (e.g., "gemini-3-flash" -> 3, "gemini-2.5-pro" -> 2.5)
    version_match = re.search(r'gemini-(\d+(?:\.\d+)?)', name)
    version = float(version_match.group(1)) if version_match else 0.0

    # Flash preferred over Pro for free tier limits
    type_rank = 0 if "flash" in name else 1

    # Stable > preview
    stability_rank = 1 if "preview" in name else 0

    return (version, type_rank, stability_rank)


def find_available_models(client: genai.Client) -> list[str]:
    """List available models and return them sorted by version descending.

    Filters out models unsuitable for vision and below MIN_MODEL_VERSION.
    """
    log("models", "Listing available Gemini models...")

    available_models = []
    try:
        for model in client.models.list():
            model_name = model.name
            if model_name.startswith("models/"):
                model_name = model_name[7:]

            if "gemini" in model_name.lower():
                available_models.append(model_name)
    except Exception as e:
        log("models", f"ERROR listing models: {e}")
        return []

    log("models", f"Found {len(available_models)} Gemini models from API")

    # Filter out models unsuitable for vision tasks
    filtered = []
    for name in available_models:
        lower = name.lower()
        if any(lower.endswith(suffix) or suffix + "-" in lower for suffix in EXCLUDED_MODEL_SUFFIXES):
            log("models", f"  Excluded (not vision): {name}")
            continue

        # Enforce minimum version
        version_match = re.search(r'gemini-(\d+(?:\.\d+)?)', name)
        version = float(version_match.group(1)) if version_match else 0.0
        if version < MIN_MODEL_VERSION:
            log("models", f"  Excluded (v{version} < {MIN_MODEL_VERSION}): {name}")
            continue

        filtered.append(name)

    # Sort: highest version first, then flash before pro, then stable before preview
    filtered.sort(key=lambda n: (-_parse_model_version(n)[0], _parse_model_version(n)[1], _parse_model_version(n)[2]))

    if filtered:
        log("models", f"Ranked 3.x+ models ({len(filtered)}): {', '.join(filtered[:5])}{'...' if len(filtered) > 5 else ''}")
    else:
        log("models", "WARNING: No Gemini 3.x+ models found! Will rely on local comparison only.")

    return filtered


def capture_rtsp_frame(rtsp_url: str) -> tuple[bytes, bytes, np.ndarray] | tuple[None, None, None]:
    """Capture a single frame from RTSP stream.

    Returns:
        Tuple of (full_frame_bytes, cropped_gate_bytes, cropped_gate_bgr) or (None, None, None)
    """
    log("camera", "Capturing frame from camera...")

    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        log("camera", "ERROR: Failed to open RTSP stream")
        return None, None, None

    ret, frame = cap.read()
    cap.release()

    if not ret:
        log("camera", "ERROR: Failed to capture frame")
        return None, None, None

    frame = cv2.resize(frame, (640, 480))
    height, width = frame.shape[:2]

    _, full_buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

    x1 = int(width * GATE_CROP["x_start"])
    x2 = int(width * GATE_CROP["x_end"])
    y1 = int(height * GATE_CROP["y_start"])
    y2 = int(height * GATE_CROP["y_end"])

    cropped = frame[y1:y2, x1:x2]
    _, crop_buffer = cv2.imencode('.jpg', cropped, [cv2.IMWRITE_JPEG_QUALITY, 90])

    log("camera", f"Frame captured: full 640x480, gate crop {x2-x1}x{y2-y1}")
    return full_buffer.tobytes(), crop_buffer.tobytes(), cropped


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
    models: list[str],
    image_data: bytes,
    reference_images: list,
    confidence_threshold: int,
) -> tuple[str, int, str]:
    """Send image to Gemini Vision 3.x and get gate status.

    Only tries 3.x models. Returns 'unavailable' if all are rate-limited
    rather than falling back to weaker models.

    Returns:
        Tuple of (status, confidence, model_used)
        status: 'open'/'closed'/'unknown'/'unavailable'
    """
    if not models:
        log("vision", "No Gemini 3.x models available, skipping API check")
        return "unavailable", 0, ""

    query_image = Image.open(io.BytesIO(image_data))
    contents = build_contents(reference_images, query_image)

    for model_name in models:
        log("vision", f"Analyzing image with {model_name}...")

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
                return "unknown", confidence, model_name

            return status, confidence, model_name

        except Exception as e:
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            is_transient = "503" in error_str or "UNAVAILABLE" in error_str or "504" in error_str or "Gateway Time-out" in error_str
            if is_rate_limit or is_transient:
                reason = "Rate limited" if is_rate_limit else "Service unavailable"
                remaining = len(models) - models.index(model_name) - 1
                log("vision", f"{reason}: {model_name}. {remaining} fallback(s) remaining.")
            else:
                log("vision", f"ERROR: {model_name} failed: {e}")
                break

    log("vision", "All 3.x models unavailable, returning safely")
    return "unavailable", 0, ""


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
    log("main", "Gate Monitor v1.3.0 starting (hybrid OpenCV + Gemini 3.x)...")

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
    ssim_threshold = config.get("ssim_threshold", SSIM_THRESHOLD)

    log("main", f"Check interval: {check_interval // 60} minutes")
    log("main", f"Confidence threshold: {confidence_threshold}%")
    log("main", f"SSIM threshold: {ssim_threshold}")
    log("main", f"Min model version: {MIN_MODEL_VERSION}")

    # Initialize Gemini client
    gemini_client = genai.Client(api_key=api_key)

    # Find available 3.x+ models
    available_models = find_available_models(gemini_client)
    if available_models:
        log("main", f"Primary model: {available_models[0]} ({len(available_models)} available)")
    else:
        log("main", "WARNING: No 3.x models found. Running in local-only mode.")

    # Load reference images (PIL for Gemini, OpenCV for SSIM)
    reference_images = load_reference_images()
    has_cv_refs = any(v is not None for v in reference_images["cv"].values())

    if not has_cv_refs:
        log("main", "WARNING: No reference images for local comparison. All checks will use API.")

    # Initialize MQTT
    mqtt_client = create_mqtt_client(config)
    publish_addon_status(mqtt_client, topic_prefix, "online")

    time.sleep(5)

    try:
        while True:
            log("main", "Starting gate check...")

            full_frame, gate_crop, gate_crop_bgr = capture_rtsp_frame(rtsp_url)

            if full_frame is None:
                log("main", "Skipping analysis due to capture failure")
                log("main", f"Next check in {check_interval // 60} minutes")
                time.sleep(check_interval)
                continue

            status = "unknown"
            confidence = 0
            method = "none"

            # Layer 1: Local SSIM comparison
            if has_cv_refs:
                local_status, local_score = compare_local(gate_crop_bgr, reference_images["cv"], ssim_threshold)

                if local_status == "closed":
                    # High confidence closed — no API needed
                    status = "closed"
                    confidence = int(local_score * 100)
                    method = "opencv"
                    log("main", f"Local: gate CLOSED (SSIM {local_score:.3f})")

                elif local_status == "open":
                    # Looks open locally — confirm with Gemini 3.x
                    log("main", f"Local: possible OPEN (SSIM {local_score:.3f}), confirming with Gemini...")
                    api_status, api_confidence, model_used = analyze_gate(
                        gemini_client, available_models, gate_crop,
                        reference_images["pil"], confidence_threshold,
                    )
                    if api_status in ("open", "closed"):
                        status = api_status
                        confidence = api_confidence
                        method = f"opencv+{model_used}"
                    elif api_status == "unavailable":
                        # API unavailable — don't trust local "open" alone, report unknown
                        log("main", "API unavailable, not trusting local OPEN detection")
                        status = "unknown"
                        method = "opencv (unconfirmed)"
                    else:
                        status = "unknown"
                        confidence = api_confidence
                        method = f"opencv+{model_used}"

                else:
                    # Inconclusive locally — ask Gemini
                    log("main", "Local inconclusive, asking Gemini 3.x...")
                    api_status, api_confidence, model_used = analyze_gate(
                        gemini_client, available_models, gate_crop,
                        reference_images["pil"], confidence_threshold,
                    )
                    if api_status == "unavailable":
                        status = "unknown"
                        method = "unavailable"
                    else:
                        status = api_status
                        confidence = api_confidence
                        method = model_used
            else:
                # No reference images — API only
                api_status, api_confidence, model_used = analyze_gate(
                    gemini_client, available_models, gate_crop,
                    reference_images["pil"], confidence_threshold,
                )
                if api_status == "unavailable":
                    status = "unknown"
                    method = "unavailable"
                else:
                    status = api_status
                    confidence = api_confidence
                    method = model_used

            # Publish result
            publish_status(mqtt_client, topic_prefix, camera_name, status)

            if status == "open":
                snapshot_path = save_snapshot(full_frame, camera_name)
                publish_alert(mqtt_client, topic_prefix, camera_name, snapshot_path)
                log("main", f"GATE IS OPEN - Alert sent [{method}] (confidence: {confidence}%)")
            else:
                log("main", f"Gate status: {status} [{method}] (confidence: {confidence}%)")

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
