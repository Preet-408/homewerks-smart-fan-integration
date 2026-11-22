"""Constants for the Homewerks Smart Fan integration."""

DOMAIN = "homewerks_smart_fan"

# Device connection
DEFAULT_PORT = 8899
UPNP_PORT = 59152
UPNP_DESCRIPTION_PORT = 49152

# Config entry keys
CONF_UDN = "udn"
CONF_UUID = "uuid"
CONF_FRIENDLY_NAME = "friendly_name"

# JSON keys for commands
KEY_FAN_POWER = "fan_power"
KEY_LIGHT_POWER = "light_power"
KEY_PERCENTAGE = "percentage"
KEY_COLOR_TEMPERATURE = "colorTemperature"
KEY_FAN_TIME = "fan_time"  # "HH:MM:SS" run timer; "00:00:00" cancels

# Values
VALUE_ON = "ON"
VALUE_OFF = "OFF"

# Color temperature range (Kelvin)
MIN_COLOR_TEMP_KELVIN = 2200
MAX_COLOR_TEMP_KELVIN = 7000

# Supported device color temperature values (device scale, inverted from Kelvin)
# Device: 7000=warm, 5500=soft, 2700=cool, 2200=daylight
SUPPORTED_DEVICE_COLOR_TEMPS = [2200, 2700, 5500, 7000]

# Brightness range
MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 100

# Polling interval (seconds)
SCAN_INTERVAL = 30

# Connection timeout (seconds)
CONNECTION_TIMEOUT = 8  # the module naps on Wi-Fi; connects can be slow

# Discovery
DISCOVERY_TIMEOUT = 2
DISCOVERY_CONCURRENCY = 50
LINKPLAY_MANUFACTURER = "Linkplay Technology Inc."

# Services
SERVICE_RUN_FAN_FOR = "run_fan_for"
SERVICE_CANCEL_FAN_TIMER = "cancel_fan_timer"
ATTR_DURATION = "duration"
