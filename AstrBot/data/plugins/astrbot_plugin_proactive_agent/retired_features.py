"""Remove only the retired reaction switch; preserve unrelated user settings."""
def migrate(config):
    key='counterattack_apology_enabled'
    if key not in config:return False
    old=config.pop(key)
    try:config.save_config()
    except BaseException:
        config[key]=old
        raise
    return True
