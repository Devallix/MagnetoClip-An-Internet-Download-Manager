from magnetoclip.config.settings import DEFAULTS, Settings


def test_defaults_loaded():
    settings = Settings()
    assert settings.get("downloads.connections_per_download") == 8
    assert settings.get("network.user_agent") == DEFAULTS["network.user_agent"]
    assert settings.get("downloads.simultaneous") == 3


def test_set_and_get():
    settings = Settings()
    settings.set("network.user_agent", "MagnetoClip-Test")
    assert settings.get("network.user_agent") == "MagnetoClip-Test"


def test_unknown_keys_ignored_on_set():
    settings = Settings()
    settings.set("does.not.exist", 42)
    assert "does.not.exist" not in settings.as_dict()


def test_unknown_keys_ignored_on_init():
    settings = Settings({"bogus": True, "appearance.theme": "light"})
    assert settings.get("appearance.theme") == "light"
    assert settings.get("bogus", "missing") == "missing"


def test_store_roundtrip_preserves_defaults():
    settings = Settings({"downloads.simultaneous": 5, "appearance.theme": "light"})
    restored = Settings.from_store(settings.to_store_dict())
    assert restored.get("downloads.simultaneous") == 5
    assert restored.get("appearance.theme") == "light"
    assert restored.get("downloads.connections_per_download") == 8


def test_default_directory_points_to_user_downloads():
    settings = Settings()
    assert settings.get("downloads.default_directory")


def test_remote_keys_defaults():
    settings = Settings()
    assert settings.get("remote.enabled") is False
    assert settings.get("remote.port") == 8477
    assert settings.get("remote.token") == ""


def test_remote_keys_store_roundtrip():
    settings = Settings(
        {"remote.enabled": True, "remote.port": 9000, "remote.token": "abc"}
    )
    restored = Settings.from_store(settings.to_store_dict())
    assert restored.get("remote.enabled") is True
    assert restored.get("remote.port") == 9000
    assert restored.get("remote.token") == "abc"
