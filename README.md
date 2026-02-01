# Gate Monitor Add-on for Home Assistant

Monitor your gate status using AI vision analysis with Claude API.

## Features

- Captures frames from RTSP camera
- Uses Claude Vision API to detect gate status (open/closed)
- Publishes status to MQTT for Home Assistant automations
- Configurable check interval (default: 30 minutes)

## Installation

1. Add this repository to Home Assistant:
   - Go to **Settings** → **Add-ons** → **Add-on Store**
   - Click the menu (⋮) → **Repositories**
   - Add: `https://github.com/fagn88/hassio-addon-gate-monitor`

2. Install the **Gate Monitor** add-on

3. Configure the add-on options:
   - `anthropic_api_key`: Your Anthropic API key
   - `rtsp_url`: Your camera's RTSP URL
   - `mqtt_username` / `mqtt_password`: MQTT credentials

4. Start the add-on

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `camera_name` | `exterior_frente` | Camera identifier |
| `rtsp_url` | - | RTSP URL of the camera |
| `check_interval_minutes` | `30` | Minutes between checks |
| `anthropic_api_key` | - | Anthropic API key |
| `mqtt_broker` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | - | MQTT username |
| `mqtt_password` | - | MQTT password |
| `mqtt_topic_prefix` | `homeassistant/gate` | MQTT topic prefix |

## MQTT Topics

| Topic | Payload | Description |
|-------|---------|-------------|
| `homeassistant/gate/{camera_name}/status` | `open` / `closed` | Current gate state |
| `homeassistant/gate/{camera_name}/alert` | `gate_open` | Alert when gate is open |
| `homeassistant/gate/status` | `online` / `offline` | Add-on status |

## Example Automation

```yaml
automation:
  - alias: "Gate Open - Notification"
    trigger:
      - platform: mqtt
        topic: "homeassistant/gate/exterior_frente/alert"
        payload: "gate_open"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "⚠️ Gate Open"
          message: "The street gate is open!"
          data:
            priority: high
            ttl: 0
```

## API Token Usage

- ~1,100 tokens per check
- ~53,000 tokens per day (48 checks)
- ~1.6M tokens per month

Consider increasing the check interval if you need to reduce API usage.

## Support

Report issues at: https://github.com/fagn88/hassio-addon-gate-monitor/issues
