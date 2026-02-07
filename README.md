# Gate Monitor Add-on for Home Assistant

Monitor your gate status using AI vision analysis with Google Gemini API.

## Features

- Captures frames from RTSP camera
- Crops image to gate region for accurate detection
- **Few-shot learning** with reference images of your specific gate
- Uses Gemini Vision API to detect gate status (open/closed)
- **Confidence scoring** with configurable threshold
- **Confirmation re-check** to eliminate false positives
- Publishes status to MQTT for Home Assistant automations
- Sends snapshot with notifications when gate is open
- Auto-selects best available Gemini model
- Configurable check interval (default: 30 minutes)

## Installation

1. Add this repository to Home Assistant:
   - Go to **Settings** → **Add-ons** → **Add-on Store**
   - Click the menu (⋮) → **Repositories**
   - Add: `https://github.com/fagn88/hassio-addon-gate-monitor`

2. Install the **Gate Monitor** add-on

3. Configure the add-on options (see Configuration below)

4. Set up reference images (see Reference Images below)

5. Start the add-on

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `camera_name` | `exterior_frente` | Camera identifier for MQTT topics |
| `rtsp_url` | - | RTSP URL of the camera (required) |
| `check_interval_minutes` | `30` | Minutes between checks |
| `gemini_api_key` | - | Google Gemini API key (required) |
| `confidence_threshold` | `70` | Minimum confidence (50-100) to accept a detection |
| `mqtt_broker` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | - | MQTT username |
| `mqtt_password` | - | MQTT password |
| `mqtt_topic_prefix` | `homeassistant/gate` | MQTT topic prefix |

### Getting a Gemini API Key

1. Go to https://aistudio.google.com/app/apikey
2. Create or sign in to your Google account
3. Click "Create API Key"
4. Copy the key and paste it in the add-on configuration

### Example RTSP URL

```
rtsp://username:password@192.168.1.100:554/stream1
```

## Reference Images (Few-Shot Learning)

For best accuracy, provide reference images of your gate in different states. The add-on uses these as labeled examples when asking Gemini to classify the current image.

### Setup

1. Create the reference directory:
   ```
   /config/www/gate-monitor/reference/
   ```

2. Add reference images (any subset works - more images = better accuracy):

   | Filename | Description |
   |----------|-------------|
   | `closed_day.jpg` | Gate closed during daytime |
   | `closed_night.jpg` | Gate closed at night |
   | `open_day.jpg` | Gate open during daytime |
   | `open_night.jpg` | Gate open at night |

3. Restart the add-on - check logs for confirmation that images were loaded

### Tips for Reference Images

- Use the **cropped gate region** from the camera (same area the add-on analyzes)
- Capture images in different lighting conditions for robustness
- The add-on works without reference images (zero-shot mode) but accuracy is significantly better with them
- You can find cropped snapshots saved by the add-on in `/config/www/gate-monitor/` to use as starting points

## MQTT Topics

| Topic | Payload | Description |
|-------|---------|-------------|
| `homeassistant/gate/{camera_name}/status` | `open` / `closed` / `unknown` | Current gate state |
| `homeassistant/gate/{camera_name}/alert` | JSON | Alert when gate is open (includes snapshot path) |
| `homeassistant/gate/status` | `online` / `offline` | Add-on status |

### Alert Payload Example

```json
{
  "event": "gate_open",
  "camera": "exterior_frente",
  "timestamp": "2026-02-01T18:45:00",
  "snapshot": "/local/gate-monitor/exterior_frente_latest.jpg?v=1706813100"
}
```

## Home Assistant Automation

Add this automation to receive notifications with the gate snapshot:

```yaml
- id: 'gate_open_notification'
  alias: "Portão Aberto - Notificação"
  triggers:
    - trigger: mqtt
      topic: "homeassistant/gate/exterior_frente/alert"
  actions:
    - action: notify.mobile_app_your_phone
      data:
        title: "⚠️ Portão Aberto"
        message: "O portão da rua está aberto!"
        data:
          image: /local/gate-monitor/exterior_frente_latest.jpg?v={{ now().timestamp() | int }}
          ttl: 0
          priority: high
  mode: single
```

## How It Works

1. **Capture**: Grabs a frame from the RTSP camera stream
2. **Crop**: Extracts only the gate region (upper-left corner by default)
3. **Analyze**: Sends cropped image to Gemini Vision API with reference images as few-shot examples
4. **Evaluate**: Parses JSON response for status and confidence score
5. **Confirm**: If gate appears OPEN, waits 15 seconds and re-checks with a fresh frame
6. **Publish**: Publishes confirmed result to MQTT
7. **Alert**: If gate is confirmed open, saves full snapshot and sends alert
8. **Wait**: Sleeps for the configured interval before next check

### Gate Region Cropping

The add-on crops the image to focus on the gate area, which:
- Improves detection accuracy
- Reduces API token usage
- Eliminates visual noise from other parts of the image

Default crop region: upper-left 25% width × 45% height

To adjust the crop region, modify `GATE_CROP` in `gate_monitor.py`:

```python
GATE_CROP = {
    "x_start": 0.0,    # Start X (0 = left edge)
    "x_end": 0.25,     # End X (0.25 = 25% from left)
    "y_start": 0.10,   # Start Y (0.10 = 10% from top)
    "y_end": 0.55,     # End Y (0.55 = 55% from top)
}
```

### Confidence Threshold

The add-on asks Gemini to return a confidence score (0-100) with each classification. Detections below the configured threshold are treated as UNKNOWN, preventing low-confidence guesses from triggering false alerts. The default threshold of 70 works well in practice - lower it if the gate is rarely detected, raise it if you still get false positives.

### Confirmation Re-check

When the gate is detected as OPEN, the add-on automatically:
1. Waits 15 seconds
2. Captures a fresh frame
3. Re-analyzes with Gemini

Only if **both** checks agree the gate is OPEN will an alert be sent. This catches momentary visual artifacts, lighting changes, or model hallucinations. The 15-second delay is acceptable for gate monitoring where immediate alerts aren't critical.

## Snapshots

Snapshots are saved to `/config/www/gate-monitor/`:
- `{camera_name}_latest.jpg` - Most recent alert snapshot
- `{camera_name}_{timestamp}.jpg` - Historical snapshots

Access via Home Assistant: `/local/gate-monitor/{filename}`

## API Token Usage

Estimated usage depends on mode:

**Zero-shot mode** (no reference images):
- ~300-500 tokens per check
- ~500,000-750,000 tokens per month (48 checks/day)

**Few-shot mode** (with 4 reference images):
- ~1,500-2,000 tokens per check
- ~2-3M tokens per month (48 checks/day)
- Confirmation re-checks only add tokens when gate appears open

All estimates are well within Gemini Flash free tier limits. The add-on automatically selects flash models which have higher free tier limits.

## Troubleshooting

### Rate Limiting (429 errors)
- The add-on automatically retries with delays (60s, 120s, 180s)
- If persistent, increase `check_interval_minutes`
- Create a new API key if daily quota is exhausted

### False Positives (Gate reported as open when closed)
1. Add reference images to `/config/www/gate-monitor/reference/`
2. Increase `confidence_threshold` (try 80 or 85)
3. Verify the gate is clearly visible in the cropped region
4. Check add-on logs for confidence scores to tune the threshold

### Wrong Detection
- Verify the gate is visible in the cropped region
- Check add-on logs to see which model is being used
- Adjust `GATE_CROP` coordinates if needed
- Add more reference images for different lighting conditions

### No Notifications
- Verify MQTT is connected (check logs)
- Ensure automation is created and enabled
- Check that snapshots are saved in `/config/www/gate-monitor/`

## Files

```
gate-monitor-addon/
├── repository.json          # Add-on repository metadata
├── README.md                 # This file
└── gate-monitor/
    ├── config.yaml           # Add-on configuration schema
    ├── build.yaml            # Multi-architecture build config
    ├── Dockerfile            # Container definition
    ├── requirements.txt      # Python dependencies
    ├── gate_monitor.py       # Main application
    ├── CHANGELOG.md          # Version history
    └── rootfs/
        └── etc/services.d/gate-monitor/
            └── run           # S6-overlay service script
```

## Support

Report issues at: https://github.com/fagn88/hassio-addon-gate-monitor/issues

## License

MIT License
