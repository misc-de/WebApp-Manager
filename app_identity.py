from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
APP_ID = 'de.cais.webappmanager'
APP_VERSION = '69'
APP_ICON_NAME = APP_ID
APP_ICON_SOURCE = APP_DIR / f'{APP_ICON_NAME}.png'
import os

APP_CONFIG_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'webapp-manager'
APP_DATA_DIR = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'webapp-manager'
APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
APP_DB_PATH = APP_DATA_DIR / 'webappmanager.db'
