from mixsync.core.capabilities import DEFAULT_ROLES, Capability


def test_default_bundles() -> None:
    assert DEFAULT_ROLES["admin"] == set(Capability)
    assert DEFAULT_ROLES["user"] == {Capability.request, Capability.download_auto}
