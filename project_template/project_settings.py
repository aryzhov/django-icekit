# Do not commit secrets to VCS.

# Environment variables will be loaded from `.env.$DOTENV` or `.env.local`.
# Local settings will be imported from `project_settings_local.py`

from icekit.project.settings.glamkit import *  # glamkit, icekit

# Override the default ICEkit settings to form project settings.


# Prepend local apps, so that their static files are used in preference.
# INSTALLED_APPS = (
# ) + INSTALLED_APPS


# To swap out the Location model
# ICEKIT_LOCATION_MODEL = 'app_label.model_name'
# ICEKIT_LOCATION_INSTALLED_APPS_STRING = 'dotted.app.path'



####### KEEP LAST
from icekit.project.settings import swappable
