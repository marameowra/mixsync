import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mixsync.__main__ import main
from mixsync.core.config import Settings
from mixsync.db.app_settings import restart_pending
from mixsync.db.auth import authenticate, hash_password
from mixsync.db.base import Base
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.auth import AppSettings, MatchingProfile, Role, User, UserRole
from mixsync.db.models.discovery import DiscoveryPlaylist
from mixsync.db.models.watchlist import Follow, WatchedEntity
from mixsync.db.settings_transfer import TransferError, export_settings, import_settings


def _roles(s: Session) -> None:
    if s.scalar(select(Role.id).limit(1)) is None:
        s.add_all([Role(name="admin"), Role(name="user")])
        s.flush()


def _source(s: Session) -> None:
    _roles(s)
    profile = MatchingProfile(
        name="standard",
        preset="balanced",
        auto_accept_max=0.07,
        review_max=0.42,
        quality_pref={"tiers": ["lossless", "mp3_320"], "floor_kbps": 192},
    )
    s.add(profile)
    s.flush()
    users = [
        User(
            username=name,
            display_name=name.title(),
            status="active",
            password_hash="hash-a",
            matching_profile_id=profile.id,
            download_auto=name == "dj",  # "second" waits for approval (#296)
        )
        for name in ("dj", "second")
    ]
    s.add_all(users)
    s.flush()
    role = s.scalar(select(Role).where(Role.name == "user"))
    assert role is not None
    s.add_all([UserRole(user_id=u.id, role_id=role.id) for u in users])
    s.add(
        AppSettings(
            id=1,
            mb_rate=0.7,
            source_priority=[["Soulseek", True], ["Usenet", False]],
            navidrome_password_encrypted="ciphertext",
            slskd_api_key_encrypted="also-secret",
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key_encrypted="prowlarr-secret",
        )
    )
    entity = WatchedEntity(kind="artist", mbid="mbid-1")
    s.add(entity)
    s.flush()
    s.add(
        Follow(
            user_id=users[0].id,
            entity_id=entity.id,
            name="Artist",
            release_types=["album"],
            min_date="2020",
            weekly_cap=4,
        )
    )
    s.add(
        DiscoveryPlaylist(
            user_id=users[0].id,
            name="Mix",
            sources=[],
            size=80,
            unowned_ratio=0.4,
            cadence_days=10,
            retention_days=45,
            keep_plays=5,
        )
    )
    s.add(
        UserLink(
            user_id=users[0].id,
            service="listenbrainz",
            external_username="dj-lb",
            secret_encrypted="link-ciphertext",
            needs_relink=False,
            verified_at=users[0].created_at,
        )
    )
    s.add(
        UserLink(
            user_id=users[0].id,
            service="discogs",
            external_username="dj-discogs",
            secret_encrypted="discogs-ciphertext",
            needs_relink=False,
            verified_at=users[0].created_at,
        )
    )


def _destination(path: Path):
    engine = make_engine(Settings(database_url=f"sqlite:///{path}"))
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    with sessions() as s, s.begin():
        _roles(s)
    return sessions


def test_export_import_round_trip_is_portable_and_preserves_per_user_profiles(
    sessions, tmp_path: Path
) -> None:
    with sessions() as source, source.begin():
        _source(source)
        document = export_settings(source)

    rendered = json.dumps(document)
    assert "ciphertext" not in rendered
    assert "prowlarr-secret" not in rendered
    assert "link-ciphertext" not in rendered
    assert "discogs-ciphertext" not in rendered
    assert "password_hash" not in rendered
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target, target.begin():
        report = import_settings(target, document, apply=True)
    assert report["rows"]["users"] == [
        {"key": "dj", "action": "add"},
        {"key": "second", "action": "add"},
    ]
    with target_sessions() as target:
        users = {u.username: u for u in target.scalars(select(User))}
        assert users["dj"].status == "disabled"
        assert users["dj"].password_hash == "!password-not-imported!"
        assert users["dj"].matching_profile_id != users["second"].matching_profile_id
        assert users["dj"].download_auto and not users["second"].download_auto
        assert target.scalar(select(func.count()).select_from(Follow)) == 1
        assert target.scalar(select(func.count()).select_from(DiscoveryPlaylist)) == 1
        links = {link.service: link for link in target.scalars(select(UserLink))}
        assert set(links) == {"listenbrainz", "discogs"}
        assert all(link.needs_relink and link.secret_encrypted == "" for link in links.values())
        assert links["discogs"].external_username == "dj-discogs"
        assert target.get(AppSettings, 1).navidrome_password_encrypted is None
        assert target.get(AppSettings, 1).mb_rate == 0.7
        assert target.get(AppSettings, 1).prowlarr_url == "http://prowlarr:9696"
        assert target.get(AppSettings, 1).prowlarr_api_key_encrypted is None
        assert target.get(AppSettings, 1).source_priority == [
            ["Soulseek", True],
            ["Usenet", False],
        ]
        saved_at = target.get(AppSettings, 1).settings_saved_at
        assert saved_at is not None
        assert "web" in restart_pending(target, None, datetime.now(UTC) + timedelta(minutes=1))


def test_import_dry_run_and_repeat_are_read_only_and_idempotent(sessions, tmp_path: Path) -> None:
    with sessions() as source, source.begin():
        _source(source)
        document = export_settings(source)
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target:
        before = target.scalar(select(func.count()).select_from(User))
        report = import_settings(target, document)
        assert report["rows"]["users"][0]["action"] == "add"
        assert target.scalar(select(func.count()).select_from(User)) == before == 0
    with target_sessions() as target, target.begin():
        import_settings(target, document, apply=True)
    with target_sessions() as target:
        report = import_settings(target, document)
        assert {row["action"] for rows in report["rows"].values() for row in rows} == {"skip"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format_version=True),
        lambda d: d.update(format_version=2),
        lambda d: d["users"][0].update(roles=["not-a-role"]),
        lambda d: d["follows"][0].update(mbid="missing"),
        lambda d: d["app_settings"].update(slskd_api_key_encrypted="secret"),
        lambda d: d["discovery_playlists"][0].update(sources=[{}]),
        lambda d: d["users"][0]["matching_profile"].update(auto_accept_max=0.9),
        lambda d: d["users"][0]["matching_profile"].update(auto_accept_max=10**400),
        lambda d: d["users"][0].update(download_auto="yes"),
        lambda d: d["app_settings"].update(source_priority="Soulseek"),
        lambda d: d["app_settings"].update(
            source_priority=[["Soulseek", True], ["Soulseek", False]]
        ),
        lambda d: d["app_settings"].update(source_priority=[["Soulseek", 1]]),
        lambda d: d["app_settings"].update(source_priority=[["Soulseek"]]),
        lambda d: d["app_settings"].update(source_priority=[["Soulseek", False]]),
        lambda d: d["app_settings"].update(trash_days=10**30),
        lambda d: d["follows"][0].update(weekly_cap=10**30),
        lambda d: d["follows"][0].update(min_date="zzzz"),
        lambda d: d["follows"][0].update(release_types=["album", "bootleg"]),
    ],
)
def test_invalid_document_is_rejected_before_any_write(sessions, tmp_path: Path, mutate) -> None:
    with sessions() as source, source.begin():
        _source(source)
        document = export_settings(source)
    mutate(document)
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target, target.begin():
        with pytest.raises(TransferError):
            import_settings(target, document, apply=True)
    with target_sessions() as target:
        assert target.scalar(select(func.count()).select_from(User)) == 0
        assert target.scalar(select(func.count()).select_from(WatchedEntity)) == 0


def test_include_secrets_requires_both_export_and_import_opt_in(sessions, tmp_path: Path) -> None:
    with sessions() as source, source.begin():
        _source(source)
        user = source.scalar(select(User).where(User.username == "dj"))
        assert user is not None
        user.password_hash = hash_password("correct horse battery staple")
        document = export_settings(source, include_secrets=True)
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target:
        with pytest.raises(TransferError, match="unknown field"):
            import_settings(target, document)
    with target_sessions() as target, target.begin():
        import_settings(target, document, apply=True, include_secrets=True)
    with target_sessions() as target:
        user = target.scalar(select(User).where(User.username == "dj"))
        assert user is not None and user.status == "active"
        assert authenticate(target, "dj", "correct horse battery staple") is user


def test_export_file_is_exclusive_and_private(settings: Settings, tmp_path: Path) -> None:
    from mixsync.db.migrate import upgrade

    upgrade()
    path = tmp_path / "settings.json"
    assert main(["export-settings", "--output", str(path)]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    assert main(["export-settings", "--output", str(path)]) == 1
    assert path.read_bytes() == before


def test_cli_import_dry_run_apply_and_repeat(
    settings: Settings, tmp_path: Path, monkeypatch, capsys
):
    from mixsync.db.migrate import upgrade

    upgrade()
    source_sessions = make_session_factory(make_engine(settings))
    with source_sessions() as source, source.begin():
        _source(source)
    exported = tmp_path / "export.json"
    assert main(["export-settings", "--output", str(exported)]) == 0

    target_path = tmp_path / "cli-target.db"
    monkeypatch.setenv("MIXSYNC_DATABASE_URL", f"sqlite:///{target_path}")
    upgrade()
    assert main(["import-settings", str(exported)]) == 0
    dry_report = json.loads(capsys.readouterr().out)
    assert dry_report["rows"]["users"][0]["action"] == "add"
    target_sessions = make_session_factory(make_engine(Settings()))
    with target_sessions() as target:
        assert target.scalar(select(func.count()).select_from(User)) == 0

    assert main(["import-settings", str(exported), "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["passwordless_users"] == ["dj", "second"]
    assert "disabled" in applied["passwordless_user_notice"]
    assert "Users page" in applied["passwordless_user_notice"]
    assert main(["import-settings", str(exported), "--apply"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert {row["action"] for rows in repeated["rows"].values() for row in rows} == {"skip"}


def test_cli_invalid_document_fails_cleanly_without_writes(
    settings: Settings, tmp_path: Path, capsys
) -> None:
    from mixsync.db.migrate import upgrade

    upgrade()
    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        '{"format_version": 1, "users": [{"username": "no-password", '
        '"display_name": null, "status": "active", "roles": ["user"], '
        '"matching_profile": null}], "app_settings": {}, '
        '"entities": [], "follows": [], "discovery_playlists": '
        '[{"username": "no-password", "name": "broken", "sources": [{}], '
        '"size": 10, "unowned_ratio": 0.5, "cadence_days": 7, '
        '"retention_days": 30, "keep_plays": 3}], "linked_accounts": []}'
    )
    assert main(["import-settings", str(invalid), "--apply"]) == 1
    output = capsys.readouterr()
    assert "sources[0]" in output.err
    assert "Traceback" not in output.err
    with make_session_factory(make_engine(settings))() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


def test_existing_user_and_singleton_settings_win_while_missing_follow_is_added(
    sessions, tmp_path: Path
) -> None:
    with sessions() as source, source.begin():
        _source(source)
        document = export_settings(source)
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target, target.begin():
        profile = MatchingProfile(
            name="local", preset="strict", auto_accept_max=0.04, review_max=0.3
        )
        target.add(profile)
        target.flush()
        role = target.scalar(select(Role).where(Role.name == "admin"))
        assert role is not None
        user = User(
            username="dj",
            display_name="Local Name",
            status="active",
            password_hash="local-hash",
            matching_profile_id=profile.id,
        )
        target.add(user)
        target.flush()
        target.add(UserRole(user_id=user.id, role_id=role.id))
        target.add(AppSettings(id=1, mb_rate=0.25))
    with target_sessions() as target, target.begin():
        report = import_settings(target, document, apply=True)
        assert report["rows"]["users"][0]["action"] == "skip"
    with target_sessions() as target:
        user = target.scalar(select(User).where(User.username == "dj"))
        assert user is not None
        assert (user.display_name, user.password_hash, user.matching_profile_id) == (
            "Local Name",
            "local-hash",
            profile.id,
        )
        assert target.get(AppSettings, 1).mb_rate == 0.25
        assert target.scalar(select(func.count()).select_from(Follow)) == 1


def test_same_preset_profiles_remain_independent_after_import(sessions, tmp_path: Path) -> None:
    with sessions() as source, source.begin():
        _source(source)
        profiles = list(source.scalars(select(MatchingProfile)))
        profiles[0].quality_pref = {"tiers": ["lossless"], "floor_kbps": None}
        profiles.append(
            MatchingProfile(
                name="standard",
                preset="balanced",
                auto_accept_max=0.07,
                review_max=0.42,
                quality_pref={"tiers": ["mp3_320"], "floor_kbps": None},
            )
        )
        source.add(profiles[1])
        source.flush()
        second = source.scalar(select(User).where(User.username == "second"))
        assert second is not None
        second.matching_profile_id = profiles[1].id
        source.flush()
        assert second.matching_profile_id == profiles[1].id
        document = export_settings(source)
        assert document["users"][1]["matching_profile"]["quality_pref"] == {
            "tiers": ["mp3_320"],
            "floor_kbps": None,
        }
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target, target.begin():
        import_settings(target, document, apply=True)
    with target_sessions() as target, target.begin():
        users = {u.username: u for u in target.scalars(select(User))}
        first = target.get(MatchingProfile, users["dj"].matching_profile_id)
        second = target.get(MatchingProfile, users["second"].matching_profile_id)
        assert first is not None and second is not None and first.id != second.id
        first.quality_pref = {"tiers": ["lossy_192"], "floor_kbps": 128}
    with target_sessions() as target:
        second = target.get(
            MatchingProfile,
            target.scalar(select(User.matching_profile_id).where(User.username == "second")),
        )
        assert second is not None
        assert second.quality_pref == {"tiers": ["mp3_320"], "floor_kbps": None}


def test_import_accepts_historical_quality_preference_defaults(sessions, tmp_path: Path) -> None:
    with sessions() as source, source.begin():
        _source(source)
        profile = source.scalar(select(MatchingProfile))
        assert profile is not None
        profile.quality_pref = {"tiers": ["lossless"]}
        document = export_settings(source)
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target, target.begin():
        import_settings(target, document, apply=True)
    with target_sessions() as target:
        profile = target.scalar(select(MatchingProfile))
        assert profile is not None
        assert profile.quality_pref == {"tiers": ["lossless"]}


def test_source_priority_empty_array_is_a_valid_saved_setting(sessions, tmp_path: Path) -> None:
    with sessions() as source, source.begin():
        _source(source)
        app = source.get(AppSettings, 1)
        assert app is not None
        app.source_priority = []
        document = export_settings(source)
        assert document["app_settings"]["source_priority"] == []
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target, target.begin():
        import_settings(target, document, apply=True)
    with target_sessions() as target:
        app = target.get(AppSettings, 1)
        assert app is not None and app.source_priority == []


def test_an_export_from_before_the_approval_queue_imports_users_as_auto_download(
    sessions, tmp_path: Path
) -> None:
    with sessions() as source, source.begin():
        _source(source)
        document = export_settings(source)
    for user in document["users"]:
        del user["download_auto"]
    target_sessions = _destination(tmp_path / "target.db")
    with target_sessions() as target, target.begin():
        import_settings(target, document, apply=True)
    with target_sessions() as target:
        assert all(u.download_auto for u in target.scalars(select(User)))


def test_cli_hostile_json_fails_cleanly_without_a_traceback(
    settings: Settings, tmp_path: Path, capsys
) -> None:
    from mixsync.db.migrate import upgrade

    upgrade()
    for name, text in (("deep.json", "[" * 100_000), ("digits.json", "1" * 5000)):
        path = tmp_path / name
        path.write_text(text)
        assert main(["import-settings", str(path), "--apply"]) == 1
        assert "Traceback" not in capsys.readouterr().err
