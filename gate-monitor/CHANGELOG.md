# Changelog

## [1.3.1] - 2026-04-06

### Fixed
- Exclude non-generateContent models: `-live`, `-image`, `-customtools`, `-audio`, `-embedding`, `-robotics`, `-computer-use`
- 404 NOT_FOUND errors now continue to next model instead of aborting the cascade

## [1.3.0] - 2026-04-06

### Added
- **Hybrid detection**: Local OpenCV SSIM comparison as first layer, Gemini API only when needed
- **Day/night auto-detection**: Uses HSV saturation to select correct reference images (color = day, grayscale = night)
- **SSIM comparison**: Structural similarity against reference images resolves ~90% of checks locally without API

### Changed
- **Gemini 3.x only**: Models below version 3.0 are excluded — returns "unknown" instead of using unreliable older models
- **No more best-of-3**: Replaced triple API calls with single confirmation when local comparison detects possible OPEN
- **Safe fallback**: When API is rate-limited/unavailable, publishes "unknown" instead of false positives
- Capture function now returns raw BGR frame for OpenCV analysis
- Reference images loaded in both PIL (Gemini) and OpenCV (SSIM) formats

### Removed
- Model fallback to Gemini 2.x and below
- Best-of-3 confirmation logic (replaced by local pre-filter + single API confirmation)

## [1.2.0] - 2026-03-30

### Added
- **Model fallback cascade**: automatically tries the next available model when the current one is unavailable (503/504) or rate-limited (429)
- Retry with backoff for transient errors (503 UNAVAILABLE, 504 Gateway Timeout) — previously only 429 was retried

### Changed
- `find_best_model()` replaced by `find_available_models()` which returns a full ranked list
- `analyze_gate()` iterates through all available models, 2 retries per model, before giving up
- Only returns error when **all** models are exhausted

## [1.1.3] - 2026-02-07

### Changed
- Updated documentation with best-of-3 confirmation logic and auto-crop details

## [1.1.2] - 2026-02-07

### Changed
- Auto-crop reference images using same GATE_CROP coordinates as query images
- Ensures reference and query images show the exact same region for accurate comparison

## [1.1.1] - 2026-02-07

### Changed
- Replaced timed re-check (15s delay) with immediate best-of-3 confirmation logic
- On OPEN: immediate 2nd check. If both agree → alert. If they disagree → 3rd tiebreaker check decides.
- Faster response and more reliable false positive elimination

## [1.1.0] - 2026-02-07

### Added
- **Few-shot reference images**: Load labeled reference images from `/config/www/gate-monitor/reference/` for visual comparison (supports `closed_day.jpg`, `closed_night.jpg`, `open_day.jpg`, `open_night.jpg`)
- **Confidence scoring**: Gemini now returns a confidence score (0-100) with each classification
- **Confidence threshold**: New `confidence_threshold` config option (default: 70) - detections below threshold are treated as UNKNOWN
- **Confirmation re-check**: When gate is detected as OPEN, automatically captures a second frame after 15 seconds and re-analyzes to eliminate false positives
- **Structured JSON responses**: Prompt now requests JSON output for reliable parsing with multiple fallback strategies

### Changed
- Improved vision prompt with explicit comparison instructions for reference images
- Set Gemini `temperature=0.0` for deterministic, consistent responses
- `analyze_gate()` now returns `(status, confidence)` tuple instead of just status
- Graceful degradation: works in zero-shot mode when no reference images are present

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
