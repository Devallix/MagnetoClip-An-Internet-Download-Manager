from magnetoclip.core.events.bus import EventBus, Events


def test_post_delivers_payload(qtbot):
    bus = EventBus()
    received = []
    bus.connect(Events.DOWNLOAD_ADDED, received.append)
    bus.post(Events.DOWNLOAD_ADDED, {"id": 1})
    assert received == [{"id": 1}]


def test_disconnect_stops_delivery(qtbot):
    bus = EventBus()
    received = []
    disconnect = bus.connect(Events.DOWNLOAD_UPDATED, received.append)
    disconnect()
    bus.post(Events.DOWNLOAD_UPDATED, {"id": 1})
    assert received == []


def test_unrelated_events_not_delivered(qtbot):
    bus = EventBus()
    received = []
    bus.connect(Events.DOWNLOAD_ADDED, received.append)
    bus.post(Events.DOWNLOAD_REMOVED, {"id": 1})
    assert received == []


def test_multiple_handlers_all_receive(qtbot):
    bus = EventBus()
    first = []
    second = []
    bus.connect(Events.SETTINGS_CHANGED, first.append)
    bus.connect(Events.SETTINGS_CHANGED, second.append)
    bus.post(Events.SETTINGS_CHANGED, {"key": "appearance.theme"})
    assert first == [{"key": "appearance.theme"}]
    assert second == [{"key": "appearance.theme"}]


def test_payload_defaults_to_none(qtbot):
    bus = EventBus()
    received = []
    bus.connect(Events.NETWORK_CHANGED, received.append)
    bus.post(Events.NETWORK_CHANGED)
    assert received == [None]
