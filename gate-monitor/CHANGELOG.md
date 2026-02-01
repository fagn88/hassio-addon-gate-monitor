# Changelog

## [1.0.11] - 2026-02-01

### Added
- Image cropping to focus on gate region for better detection accuracy
- Configurable crop coordinates via `GATE_CROP` constant

### Changed
- Sends only cropped gate region to Gemini (reduces tokens, improves accuracy)
- Keeps full frame for notification snapshots
- Simplified vision prompt focused on gate orientation

## [1.0.10] - 2026-02-01

### Changed
- Improved vision prompt with gate location details

## [1.0.9] - 2026-02-01

### Fixed
- Save snapshots to `/config/www/` instead of `/media/` for notification compatibility
- Use `/local/` path which works with HA mobile notifications
- Add cache-busting timestamp to image URLs

## [1.0.8] - 2026-02-01

### Added
- Save snapshot when gate is open
- Include snapshot path in MQTT alert payload (JSON format)
- Save both timestamped and "latest" versions of snapshots
- Media folder mapping for snapshot storage

## [1.0.7] - 2026-02-01

### Changed
- Prioritize flash models (higher free tier limits)
- Add automatic retry with exponential backoff on 429 rate limit errors

## [1.0.6] - 2026-02-01

### Changed
- Add Gemini 3 models to preference list

## [1.0.5] - 2026-02-01

### Added
- Auto-detect available Gemini models at startup
- Automatically select best model from preference list
- Log all available models for debugging

## [1.0.4] - 2026-02-01

### Fixed
- Use correct model name `gemini-1.5-pro`

## [1.0.3] - 2026-02-01

### Changed
- Switch to new `google-genai` SDK (replaces deprecated `google-generativeai`)
- Use Gemini 2.5 Pro model

## [1.0.2] - 2026-02-01

### Changed
- Switch from Claude/Anthropic to Google Gemini Vision API
- Use `gemini-1.5-flash` model
- Update config to use `gemini_api_key`

## [1.0.1] - 2026-02-01

### Fixed
- Add execute permission to S6-overlay run script

## [1.0.0] - 2026-02-01

### Added
- Initial release
- RTSP camera frame capture with OpenCV
- Claude Vision API integration for gate status detection
- MQTT publishing for Home Assistant automations
- S6-overlay service management
- Multi-architecture support (aarch64, amd64, armhf, armv7, i386)
