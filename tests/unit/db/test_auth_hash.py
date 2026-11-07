from mixsync.db.auth import hash_password, verify_password


def test_password_verify() -> None:
    h = hash_password("secret")
    assert verify_password(h, "secret")
    assert not verify_password(h, "nope")
    assert not verify_password("garbage", "secret")
