DOMAIN = "mosru_water"

CONF_PAYCODE         = "paycode"
CONF_FLAT            = "flat"
# ed.mos.ru адресует квартиру своим userPlaceId, а не кодом плательщика.
# Для записей, созданных до перехода на ed.mos.ru, определяется автоматически
# по paycode и сохраняется в config entry.
CONF_USER_PLACE_ID   = "user_place_id"
CONF_COLD_ID         = "cold_counter_id"
CONF_HOT_ID          = "hot_counter_id"
CONF_COLD_ENTITY     = "cold_entity"
CONF_HOT_ENTITY      = "hot_entity"
CONF_SUBMIT_DAY      = "submit_day"
CONF_SESSION_COOKIES = "session_cookies"

UNIT_M3               = "m³"
UPDATE_INTERVAL_HOURS = 1
