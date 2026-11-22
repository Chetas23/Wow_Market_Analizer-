# config.example.py
# Copia este archivo como config.py y rellena tus credenciales de Battle.net

CLIENT_ID = "TU_CLIENT_ID_AQUI"
CLIENT_SECRET = "TU_CLIENT_SECRET_AQUI"

REGION = "eu"  # "eu" o "us"
LOCALE = "es_ES"  # "es_ES", "en_GB", "en_US", etc.

NAMESPACE_DYNAMIC = f"dynamic-forever-{REGION}"
NAMESPACE_STATIC = f"static-forever-{REGION}"
BASE_URL = f"https://{REGION}.api.blizzard.com"
TOKEN_URL = "https://oauth.battle.net/token"

DB_PATH = "wow_market.sqlite"
INITIAL_DAYS = 3