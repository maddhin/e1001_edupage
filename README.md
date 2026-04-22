# E1001 E-ink School Display for Home Assistant

A battery-powered E-ink display in the kitchen that shows your child's school schedule, weather forecast, alarm times, and calendar events — updated automatically and integrated with Home Assistant.

<!-- Add a screenshot here -->
<!-- ![E1001 Display](docs/screenshot.jpg) -->

---

## Overview

The display wakes up from deep sleep, downloads a rendered PNG of a Lovelace dashboard view (via a headless browser / Puppet), shows it on screen, and goes back to sleep. The sleep duration is dynamically adjusted based on the current school status — short intervals right before school starts, longer during the night.

The school schedule is fetched from [Edupage](https://www.edupage.org/) using a Python script and exposed to Home Assistant as a `command_line` sensor.

## Features
- gets childs school schedule directly from Edupage API and makes the relevant information available in sensor school status -> with this sensor you can start automations and so on
- the school sensor delivers also a summary of the school day as speech text which can be used e.g. for the morning alert to read out today's schedule.
- because of the Edupage API calls, the sensor is always up to date and you get the latest changes in schedule (on the display!). 
- deeply in HA integrated Seeedstudio E1001 epaper display. The display fetches the URL and sleep times from HA, service mode can be switched on from HA,...
- the display shows service mode, last updated and battery status on top of the puppet dashboard
- the display dashboard primarily shows a status windows (school times, countdown to school start,...) and todays/tomorrows school schedule incl. an icon if today/tomorrow is PE and (more or less) live changes in schedule. All this is based on the current status of the school status sensor. So this is dynamic. So, one can put special messages for weekends, etc. Besides this, it shows todays and tomorrows calendar events, the weather for the next couple of ours and the next alerts (currently only for debugging)

It took qiute some fiddeling to make all that work and almost everything is working stable. Of course, there is absolutely plenty of room to make all this look more pretty. I have been focusing of getting all the features I wanted to work. Now one can play with the edupage infos and display. 

There is one this I haven't completely figured out: the school schedule is sometimes not showing directly after a HA restart. I have been tryiong to make this more robust but failed so far. If you have a good idea, please drop a message. 

Where I have completely failed is to make the grayscale work on the E1001 display. I read it is supposed to have 4 gray scales but none of the options I tried worked out. Hence a b/w optimised display. I will update this here, if I figure it out (or somebody shares a solution to me).

### What it shows
<img width="1106" height="768" alt="e1001_edupage (Mittel)" src="https://github.com/user-attachments/assets/b1797d6b-06ce-444e-9023-6d2fbe9b118e" />


| Area | Content |
|---|---|
| Top-left | School status / countdown / info box |
| Bottom-left | Today's or tomorrow's timetable with substitution notes |
| Top-right | 7-hour weather forecast |
| Middle-right | Next school alarm time |
| Bottom-right | Calendar events (today & tomorrow) |

---

## Hardware

- **[Seeedstudio reTerminal E1001](https://www.seeedstudio.com/reTerminal-E1001-p-6534.html)** — ESP32-S3 + 7.5" monochrome ePaper display (800×480 px)
- LiPo battery (optional, for untethered operation)
- Green button connected to GPIO3 (manual refresh trigger)

The ESPHome firmware uses the `waveshare_epaper` platform with `model: 7.50inv2`.

---

## Software / Dependencies

### Home Assistant
- [Home Assistant](https://www.home-assistant.io/) (any recent version)
- [HACS](https://hacs.xyz/) for frontend components and integrations

### Required HACS frontend components
| Component | Purpose |
|---|---|
| [html-template-card](https://github.com/PiotrMachowski/Home-Assistant-Lovelace-HTML-Jinja2-Template-card) | Info box and schedule cards |
| [button-card](https://github.com/custom-cards/button-card) | Weather and calendar cards |
| [decluttering-card](https://github.com/custom-cards/decluttering-card) | Reusable weather slot template |
| [card-mod](https://github.com/thomasloven/lovelace-card-mod) | Card styling |
| [grid-layout](https://github.com/enfoiro/lovelace-grid-layout) | Two-column dashboard layout |
| [stack-in-card](https://github.com/custom-cards/stack-in-card) | Horizontal weather stack |

### Required HACS integration
| Integration | Purpose |
|---|---|
| [homeassistantedupage](https://github.com/hpo13/Haas-Edupage-simple) | Provides `sensor.school_schedule` via Edupage API |

> **Note:** `homeassistantedupage` must be installed and configured before the command_line sensor will work. The Python script in this repo is an alternative/supplement that also reads the Edupage API directly.

### Python (for the schedule script)
```
pip install edupage-api pyyaml urllib3
```

### Headless browser (for PNG rendering)
The display downloads a PNG image of the dashboard view. You need a tool like:
- [Browserless](https://www.browserless.io/) / [Puppet](https://github.com/nicktindall/cycopic) running locally
- Any service that renders a URL to a PNG at `800×480 px`

Set the rendered URL in `input_text.eink_url` (see Step 8).

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Home Assistant                  │
│                                                 │
│  command_line sensor: sensor.school_schedule    │
│    └─ runs edupage_schedule.py every hour       │
│       └─ reads /config/secrets.yaml             │
│                                                 │
│  Template sensors (templates.yaml)              │
│    sensor.school_status  ← drives sleep time    │
│    sensor.school_start_today/tomorrow           │
│    sensor.school_alarm_today/tomorrow           │
│    sensor.eink_weather_snapshot                 │
│                                                 │
│  Automations (automations.yaml)                 │
│    ├─ Update schedule at midnight + mornings    │
│    └─ Set sleep duration from school_status     │
│                                                 │
│  Lovelace dashboard → headless browser → PNG    │
│         ↑ URL stored in input_text.eink_url     │
└───────────────────────┬─────────────────────────┘
                        │ ESPHome API
                        ▼
              ┌─────────────────┐
              │  E1001 Display  │
              │  (ESP32-S3)     │
              │                 │
              │  1. Connect     │
              │  2. Read URL    │
              │  3. Download PNG│
              │  4. Show image  │
              │  5. Deep sleep  │
              └─────────────────┘
```

---

## Setup Guide

### Step 1: Create Home Assistant helpers

Use `home-assistant/helpers.yaml` as a [HA Package](https://www.home-assistant.io/docs/configuration/packages/):

```yaml
# configuration.yaml
homeassistant:
  packages:
    eink: !include home-assistant/helpers.yaml
```

Or create the three helpers manually under **Settings → Devices & Services → Helpers**:

| Helper | Type | ID |
|---|---|---|
| E-ink Maintenance Mode | Toggle (input_boolean) | `eink_maintenance_mode` |
| E-ink Sleep Duration | Number (0–120 min) | `eink_sleep_duration` |
| School Alarm Lead Time | Number (0–120 min) | `school_alarm_lead_time` |
| E-ink Display URL | Text | `eink_url` |

### Step 2: Add templates

Copy the relevant blocks from `home-assistant/templates.yaml` into your `templates.yaml`, or include the file:

```yaml
# configuration.yaml
template: !include home-assistant/templates.yaml
```

> Adjust the `weather.forecast_home` entity ID to match your weather integration.

### Step 3: Add automations

Copy the entries from `home-assistant/automations.yaml` into your `automations.yaml`.

### Step 4: Install the Python script

Copy `python/edupage_schedule.py` to `/config/scripts/edupage_schedule.py` on your Home Assistant host.

Create a Python virtual environment (optional but recommended):
```bash
cd /config/scripts
python3 -m venv venv
source venv/bin/activate
pip install edupage-api pyyaml urllib3
```

If using a venv, update the command in `command_line.yaml`:
```yaml
command: "/config/scripts/venv/bin/python3 /config/scripts/edupage_schedule.py"
```

Add the required secrets to `/config/secrets.yaml`:
```yaml
edupage_username:   your.email@example.com
edupage_password:   your_password
edupage_subdomain:  your-school      # e.g. "mygym" for mygym.edupage.org
edupage_child_name: Child Name       # as it appears in Edupage (optional)
edupage_manual_class: "4a"           # manual class override (optional)
```

### Step 5: Add the command_line sensor

```yaml
# configuration.yaml
command_line: !include home-assistant/command_line.yaml
```

Restart Home Assistant. The sensor `sensor.school_schedule` should appear and populate after ~60 seconds.

### Step 6: Adapt the schedule

Edit `python/edupage_schedule.py` to match your school:

- **`PERIODS`** — adjust lesson start/end times
- **`SUBJECT_MAPPING`** — map Edupage subject codes to display names
- **`HOLIDAY_TITLES`** — add holiday names that appear in your school's Edupage

### Step 7: Set up the dashboard

1. In Home Assistant, create a new dashboard and configure it with **Manual YAML mode**
2. Paste the content of `home-assistant/dashboards/dashboard.yaml`
3. Make sure all card files are accessible (adjust `!include` paths if needed)
4. **Update `card_calendar.yaml`**: replace `calendar.your_family_calendar` with your actual calendar entity ID

### Step 8: Set up the headless browser (PNG rendering)

Install a headless browser service that can render a URL to a PNG image at 800×480 px (e.g. Browserless, Puppeteer, or a similar tool). Configure it to render the Lovelace dashboard view.

Set the render URL in Home Assistant:

```
Settings → Devices & Services → Helpers → E-ink Display URL
```

Example URL format:
```
http://YOUR_HA_IP:10000/e1001?viewport=800x480&colors=000000%2CFFFFFF%2C555555
```

### Step 9: Flash ESPHome firmware

1. Copy `esphome/e1001.yaml` to your ESPHome configuration directory
2. Add the required secrets to your ESPHome `secrets.yaml`:

```yaml
api_encryption_key:  your_32_byte_base64_key
ota_password:        your_ota_password
wifi_ssid:           YourWiFiNetwork
wifi_password:       your_wifi_password
fallback_ap_password: fallback_password
```

3. Build and flash: `esphome run esphome/e1001.yaml`
4. The device will connect to HA, read `input_text.eink_url`, download the PNG and display it

---

## Configuration Reference

### secrets.yaml keys (for the Python script)

| Key | Description |
|---|---|
| `edupage_username` | Login email for Edupage |
| `edupage_password` | Login password |
| `edupage_subdomain` | School subdomain (e.g. `myschool` for `myschool.edupage.org`) |
| `edupage_child_name` | Child's full name for student lookup (optional) |
| `edupage_manual_class` | Manual class name override (e.g. `"4a"`) — use if auto-detection fails |

### ESPHome secrets.yaml keys

| Key | Description |
|---|---|
| `api_encryption_key` | 32-byte base64 key for HA API encryption |
| `ota_password` | OTA update password |
| `wifi_ssid` | WiFi network name |
| `wifi_password` | WiFi password |
| `fallback_ap_password` | Password for fallback access point |

---

## School Status Logic

`sensor.school_status` can be in one of these states:

| State | Meaning |
|---|---|
| `school_today` | Normal school day, early morning (before countdown) |
| `school_soon` | School starts in < 60 min (countdown phase) |
| `school_running` | School is currently in session |
| `school_ending_soon` | School ended < 15 min ago |
| `preview_tomorrow` | Afternoon/evening — showing tomorrow's schedule |
| `friday_weekend` | Friday after school — weekend mode |
| `weekend` | Saturday, or Sunday before 13:00 |
| `holiday` | School holiday today |
| `public_holiday` | Public holiday today |
| `holiday_tomorrow` | School holiday starts tomorrow |
| `public_holiday_tomorrow` | Public holiday tomorrow |
| `no_school_tomorrow` | No school tomorrow (other reason) |
| `no_school_today` | No school today (fallback) |

The `refresh_interval` attribute of this sensor drives `input_number.eink_sleep_duration`, which controls how long the display sleeps between wake-ups.

---

## Customization

### Adapting lesson times
Edit the `PERIODS` dictionary in `python/edupage_schedule.py`:
```python
PERIODS = {
    "1": {"start": "07:55", "end": "08:40"},
    "2": {"start": "08:40", "end": "09:25"},
    # ...
}
```

### Adapting subject names
Edit `SUBJECT_MAPPING` — keys are Edupage codes/names, values are display names:
```python
SUBJECT_MAPPING = {
    "M":  "Math",
    "E":  "English",
    "PE": "Sport",
}
```

### Adapting holiday detection
Edit `HOLIDAY_TITLES` — add the exact holiday names that appear in your school's Edupage substitution plan.

### Adjusting refresh intervals
Edit the `cfg` variables block in the `sensor.school_status` template in `home-assistant/templates.yaml`:

```yaml
cfg:
  p_start: 60   # countdown starts this many minutes before school
  i_bald: 3     # refresh every 3 min during countdown
  i_schule: 15  # refresh every 15 min during school hours
  i_nacht: 45   # refresh every 45 min at night
```

---

