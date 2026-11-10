"""Portable settings export/import. Deliberately excludes runtime state and credentials."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from mixsync.core.config import Settings
from mixsync.db.app_settings import EDITABLE
from mixsync.db.models.accounts import UserLink
from mixsync.db.models.auth import AppSettings, MatchingProfile, Role, User, UserRole
from mixsync.db.models.discovery import DiscoveryPlaylist
from mixsync.db.models.watchlist import Follow, WatchedEntity
from mixsync.watchlist.follows import RELEASE_TYPES

FORMAT_VERSION = 1
MAX_INT = 2**31 - 1  # every integer column fits; larger values would fail mid-write
_DATE = re.compile(r"\d{4}(?:-\d{2}(?:-\d{2})?)?")  # as the watchlist form accepts
_APP_FIELDS = frozenset((*EDITABLE, "min_auto_accept_confidence", "source_priority"))
_PROFILE_FIELDS = frozenset({"name", "preset", "auto_accept_max", "review_max", "quality_pref"})
_USER_FIELDS = frozenset(
    {
        "username",
        "display_name",
        "status",
        "roles",
        "download_auto",
        "password_hash",
        "matching_profile",
    }
)
_ENTITY_FIELDS = frozenset({"kind", "mbid"})
_FOLLOW_FIELDS = frozenset(
    {"username", "kind", "mbid", "name", "release_types", "min_date", "weekly_cap"}
)
_PLAYLIST_FIELDS = frozenset(
    {
        "username",
        "name",
        "sources",
        "size",
        "unowned_ratio",
        "cadence_days",
        "retention_days",
        "keep_plays",
    }
)
_LINK_FIELDS = frozenset({"username", "service", "external_username"})


class TransferError(ValueError):
    pass


def _profile_dict(row: MatchingProfile) -> dict[str, Any]:
    return {name: getattr(row, name) for name in _PROFILE_FIELDS}


def export_settings(s: Session, *, include_secrets: bool = False) -> dict[str, Any]:
    """Return portable configuration; password hashes are included only by explicit opt-in."""
    users: list[dict[str, Any]] = []
    for user in s.scalars(select(User).order_by(User.username)):
        roles = s.scalars(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
            .order_by(Role.name)
        ).all()
        profile = (
            s.get(MatchingProfile, user.matching_profile_id) if user.matching_profile_id else None
        )
        user_row: dict[str, Any] = {
            "username": user.username,
            "display_name": user.display_name,
            "status": user.status,
            "roles": roles,
            "download_auto": user.download_auto,
            "matching_profile": _profile_dict(profile) if profile else None,
        }
        if include_secrets:
            user_row["password_hash"] = user.password_hash
        users.append(user_row)

    app = s.get(AppSettings, 1)
    app_values = {
        name: getattr(app, name)
        for name in _APP_FIELDS
        if app is not None and getattr(app, name) is not None
    }
    entities = [
        {"kind": e.kind, "mbid": e.mbid}
        for e in s.scalars(select(WatchedEntity).order_by(WatchedEntity.kind, WatchedEntity.mbid))
    ]
    follows: list[dict[str, Any]] = []
    follow_rows = cast(
        list[Follow],
        s.scalars(
            select(Follow)
            .join(User, Follow.user_id == User.id)
            .join(WatchedEntity, Follow.entity_id == WatchedEntity.id)
            .order_by(User.username, WatchedEntity.kind, WatchedEntity.mbid)
        ).all(),
    )
    for row in follow_rows:
        user = s.get(User, row.user_id)
        entity = s.get(WatchedEntity, row.entity_id)
        assert user is not None and entity is not None
        follows.append(
            {
                "username": user.username,
                "kind": entity.kind,
                "mbid": entity.mbid,
                "name": row.name,
                "release_types": row.release_types,
                "min_date": row.min_date,
                "weekly_cap": row.weekly_cap,
            }
        )
    playlists: list[dict[str, Any]] = []
    playlist_rows = cast(
        list[DiscoveryPlaylist],
        s.scalars(
            select(DiscoveryPlaylist)
            .join(User, DiscoveryPlaylist.user_id == User.id)
            .order_by(User.username, DiscoveryPlaylist.name)
        ).all(),
    )
    for row in playlist_rows:
        user = s.get(User, row.user_id)
        assert user is not None
        playlists.append(
            {
                "username": user.username,
                "name": row.name,
                "sources": row.sources,
                "size": row.size,
                "unowned_ratio": row.unowned_ratio,
                "cadence_days": row.cadence_days,
                "retention_days": row.retention_days,
                "keep_plays": row.keep_plays,
            }
        )
    links: list[dict[str, Any]] = []
    link_rows = cast(
        list[UserLink],
        s.scalars(
            select(UserLink)
            .join(User, UserLink.user_id == User.id)
            .order_by(User.username, UserLink.service)
        ).all(),
    )
    for row in link_rows:
        user = s.get(User, row.user_id)
        assert user is not None
        links.append(
            {
                "username": user.username,
                "service": row.service,
                "external_username": row.external_username,
            }
        )
    return {
        "format_version": FORMAT_VERSION,
        "users": users,
        "app_settings": app_values,
        "entities": entities,
        "follows": follows,
        "discovery_playlists": playlists,
        "linked_accounts": links,
    }


def _object(
    value: Any, path: str, fields: frozenset[str], required: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TransferError(f"{path}: expected an object")
    obj = cast(dict[str, Any], value)
    unknown: set[str] = set(obj) - fields
    missing = required - set(obj)
    if unknown:
        raise TransferError(f"{path}: unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise TransferError(f"{path}: missing field(s): {', '.join(sorted(missing))}")
    return obj


def _string(
    value: Any,
    path: str,
    *,
    nullable: bool = False,
    allow_empty: bool = False,
    max_length: int | None = None,
) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or (not value and not allow_empty):
        raise TransferError(f"{path}: expected a string")
    if max_length is not None and len(value) > max_length:
        raise TransferError(f"{path}: exceeds maximum length {max_length}")
    return value


def _int(value: Any, path: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int:
        raise TransferError(f"{path}: expected an integer")
    if abs(value) > MAX_INT:
        raise TransferError(f"{path}: integer is outside the supported range")
    return value


def _number(value: Any, path: str, *, nullable: bool = False) -> float | None:
    if nullable and value is None:
        return None
    if type(value) not in (int, float):
        raise TransferError(f"{path}: expected a number")
    try:
        return float(value)
    except OverflowError:
        raise TransferError(f"{path}: number is outside the supported range") from None


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise TransferError(f"{path}: expected an array")
    return cast(list[Any], value)


def _validate_profile(value: Any, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    d = _object(value, path, _PROFILE_FIELDS, _PROFILE_FIELDS)
    _string(d["name"], f"{path}.name", max_length=100)
    if not isinstance(d["preset"], str) or d["preset"] not in {
        "strict",
        "balanced",
        "loose",
        "custom",
    }:
        raise TransferError(f"{path}.preset: unknown profile preset")
    for name in ("auto_accept_max", "review_max"):
        threshold = _number(d[name], f"{path}.{name}")
        if threshold is None or not 0 <= threshold <= 1:
            raise TransferError(f"{path}.{name}: expected a number from 0 to 1")
    if d["auto_accept_max"] > d["review_max"]:
        raise TransferError(f"{path}: auto_accept_max must not exceed review_max")
    quality = d["quality_pref"]
    if quality is not None:
        if not isinstance(quality, dict):
            raise TransferError(f"{path}.quality_pref: expected tiers and floor_kbps")
        quality = cast(dict[str, Any], quality)
        if set(quality) - {"tiers", "floor_kbps"}:
            raise TransferError(f"{path}.quality_pref: contains unsupported fields")
        tiers = quality.get("tiers", [])
        if not isinstance(tiers, list):
            raise TransferError(f"{path}.quality_pref.tiers: contains an unsupported quality tier")
        tiers = cast(list[Any], tiers)
        if any(
            not isinstance(t, str)
            or t not in {"lossless", "mp3_320", "mp3_v0", "mp3_256", "lossy_192"}
            for t in tiers
        ):
            raise TransferError(f"{path}.quality_pref.tiers: contains an unsupported quality tier")
        if len(set(tiers)) != len(tiers):
            raise TransferError(f"{path}.quality_pref.tiers: duplicate quality tier")
        floor = quality.get("floor_kbps")
        if floor is not None and (type(floor) is not int or not 1 <= floor <= 2000):
            raise TransferError(
                f"{path}.quality_pref.floor_kbps: expected null or an integer from 1 to 2000"
            )
    return d


def _validate_document(
    document: Any, *, include_secrets: bool, known_roles: set[str]
) -> dict[str, Any]:
    root = _object(
        document,
        "$",
        frozenset(
            {
                "format_version",
                "users",
                "app_settings",
                "entities",
                "follows",
                "discovery_playlists",
                "linked_accounts",
            }
        ),
        frozenset(
            {
                "format_version",
                "users",
                "app_settings",
                "entities",
                "follows",
                "discovery_playlists",
                "linked_accounts",
            }
        ),
    )
    version = root["format_version"]
    if type(version) is not int:
        raise TransferError("$.format_version: expected an integer")
    if version != FORMAT_VERSION:
        raise TransferError(
            f"$.format_version: unsupported version {version}; "
            f"supported version is {FORMAT_VERSION}"
        )
    users: list[dict[str, Any]] = []
    usernames: set[str] = set()
    user_fields = _USER_FIELDS if include_secrets else _USER_FIELDS - {"password_hash"}
    for i, item in enumerate(_list(root["users"], "$.users")):
        path = f"$.users[{i}]"
        d = _object(
            item,
            path,
            user_fields,
            frozenset({"username", "display_name", "status", "roles", "matching_profile"}),
        )
        username = _string(d["username"], f"{path}.username", max_length=100)
        assert username is not None
        if username in usernames:
            raise TransferError(f"{path}.username: duplicate username")
        usernames.add(username)
        _string(
            d["display_name"],
            f"{path}.display_name",
            nullable=True,
            allow_empty=True,
            max_length=100,
        )
        if not isinstance(d["status"], str) or d["status"] not in {"active", "disabled"}:
            raise TransferError(f"{path}.status: expected active or disabled")
        roles = _list(d["roles"], f"{path}.roles")
        if not roles or any(not isinstance(r, str) or r not in known_roles for r in roles):
            raise TransferError(f"{path}.roles: contains an unknown or missing role")
        if len(set(roles)) != len(roles):
            raise TransferError(f"{path}.roles: duplicate role")
        if "download_auto" in d and type(d["download_auto"]) is not bool:
            raise TransferError(f"{path}.download_auto: expected a boolean")
        _validate_profile(d["matching_profile"], f"{path}.matching_profile")
        if "password_hash" in d:
            _string(d["password_hash"], f"{path}.password_hash", max_length=255)
        users.append(d)
    app = _object(root["app_settings"], "$.app_settings", _APP_FIELDS)
    normalized_app: dict[str, Any] = {}
    for name, value in app.items():
        try:
            if name == "min_auto_accept_confidence":
                if value is not None and (type(value) is not int or not 0 <= value <= 100):
                    raise TransferError(
                        f"$.app_settings.{name}: expected null or an integer from 0 to 100"
                    )
            elif name == "source_priority":
                if value is not None:
                    if not isinstance(value, list):
                        raise TransferError(
                            "$.app_settings.source_priority: expected null or an array"
                        )
                    pairs = cast(list[Any], value)
                    names: set[str] = set()
                    for i, raw_pair in enumerate(pairs):
                        pair = cast(list[Any], raw_pair)
                        if not isinstance(raw_pair, list) or len(pair) != 2:
                            raise TransferError(
                                f"$.app_settings.source_priority[{i}]: expected a name and "
                                "boolean pair"
                            )
                        name_value, enabled = pair
                        if not isinstance(name_value, str) or not name_value:
                            raise TransferError(
                                f"$.app_settings.source_priority[{i}][0]: expected a non-empty "
                                "source name"
                            )
                        if type(enabled) is not bool:
                            raise TransferError(
                                f"$.app_settings.source_priority[{i}][1]: expected a boolean"
                            )
                        if name_value in names:
                            raise TransferError(
                                "$.app_settings.source_priority: duplicate source name "
                                f"{name_value!r}"
                            )
                        names.add(name_value)
                    if pairs and not any(enabled for _, enabled in pairs):
                        raise TransferError(  # as the Sources page: none enabled finds nothing
                            "$.app_settings.source_priority: at least one source must be enabled"
                        )
                    normalized_app[name] = value
            else:
                from pydantic import TypeAdapter

                annotation = Settings.model_fields[name].annotation
                assert annotation is not None
                typed = TypeAdapter(annotation).validate_python(value, strict=True)
                normalized_app[name] = getattr(Settings(**{name: typed}), name)
                if type(normalized_app[name]) is int and abs(normalized_app[name]) > MAX_INT:
                    raise TransferError(f"$.app_settings.{name}: integer is outside the range")
        except ValidationError as e:
            details = "; ".join(
                f"{err['loc']}: {err['msg']}" for err in e.errors(include_input=False)
            )
            raise TransferError(f"$.app_settings.{name}: {details}") from None
    normalized_app.update(
        {"min_auto_accept_confidence": app["min_auto_accept_confidence"]}
        if "min_auto_accept_confidence" in app
        else {}
    )
    root["app_settings"] = normalized_app
    entities: list[dict[str, Any]] = []
    entity_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(_list(root["entities"], "$.entities")):
        path = f"$.entities[{i}]"
        d = _object(item, path, _ENTITY_FIELDS, _ENTITY_FIELDS)
        kind, mbid = (
            _string(d["kind"], f"{path}.kind"),
            _string(d["mbid"], f"{path}.mbid", max_length=36),
        )
        if kind not in {"artist", "label"}:
            raise TransferError(f"{path}.kind: expected artist or label")
        key = (kind, mbid or "")
        if key in entity_keys:
            raise TransferError(f"{path}: duplicate entity")
        entity_keys.add(key)
        entities.append(d)
    for key, fields, required in (
        ("follows", _FOLLOW_FIELDS, _FOLLOW_FIELDS),
        ("discovery_playlists", _PLAYLIST_FIELDS, _PLAYLIST_FIELDS),
        ("linked_accounts", _LINK_FIELDS, _LINK_FIELDS),
    ):
        seen: set[tuple[Any, ...]] = set()
        for i, item in enumerate(_list(root[key], f"$.{key}")):
            path = f"$.{key}[{i}]"
            d = _object(item, path, fields, required)
            username = _string(d["username"], f"{path}.username", max_length=100)
            if username not in usernames:
                raise TransferError(f"{path}.username: references an unknown user")
            if key == "follows":
                kind = _string(d["kind"], f"{path}.kind")
                mbid = _string(d["mbid"], f"{path}.mbid", max_length=36)
                if (kind, mbid) not in entity_keys:
                    raise TransferError(f"{path}: references an unknown entity")
                _string(d["name"], f"{path}.name", max_length=255)
                if kind not in {"artist", "label"}:
                    raise TransferError(f"{path}.kind: expected artist or label")
                release_types = _list(d["release_types"], f"{path}.release_types")
                if any(x not in RELEASE_TYPES for x in release_types):
                    raise TransferError(
                        f"{path}.release_types: expected values from {', '.join(RELEASE_TYPES)}"
                    )
                min_date = _string(
                    d["min_date"],
                    f"{path}.min_date",
                    nullable=True,
                    allow_empty=True,
                    max_length=10,
                )
                if min_date and not _DATE.fullmatch(min_date):
                    raise TransferError(f"{path}.min_date: expected YYYY, YYYY-MM or YYYY-MM-DD")
                cap = _int(d["weekly_cap"], f"{path}.weekly_cap", nullable=True)
                if cap is not None and cap < 1:
                    raise TransferError(f"{path}.weekly_cap: expected null or a positive integer")
                identity = (username, kind, mbid)
            elif key == "discovery_playlists":
                _string(d["name"], f"{path}.name", max_length=255)
                if not isinstance(d["sources"], list):
                    raise TransferError(f"{path}.sources: expected an array")
                sources = cast(list[Any], d["sources"])
                source_types: list[str] = []
                for j, raw_source in enumerate(sources):
                    source = raw_source
                    if not isinstance(source, dict):
                        raise TransferError(f"{path}.sources[{j}]: expected a source object")
                    source = cast(dict[str, Any], source)
                    source_type = source.get("type")
                    if not isinstance(source_type, str):
                        raise TransferError(f"{path}.sources[{j}]: expected a source type")
                    source_types.append(source_type)
                    expected = {"type", "prompt", "mode"} if source_type == "lb_radio" else {"type"}
                    if (
                        source_type not in {"weekly_exploration", "recommendations", "lb_radio"}
                        or set(source) != expected
                    ):
                        raise TransferError(
                            f"{path}.sources[{j}]: unsupported source fields or type"
                        )
                    if source_type == "lb_radio" and (
                        not isinstance(source["prompt"], str)
                        or not source["prompt"].strip()
                        or not isinstance(source["mode"], str)
                        or source["mode"] not in {"easy", "medium", "hard"}
                    ):
                        raise TransferError(f"{path}.sources[{j}]: invalid radio prompt or mode")
                if len(set(source_types)) != len(source_types):
                    raise TransferError(f"{path}.sources: duplicate sources are not supported")
                size = _int(d["size"], f"{path}.size")
                ratio = _number(d["unowned_ratio"], f"{path}.unowned_ratio")
                cadence = _int(d["cadence_days"], f"{path}.cadence_days")
                retention = _int(d["retention_days"], f"{path}.retention_days")
                plays = _int(d["keep_plays"], f"{path}.keep_plays")
                if None in (size, ratio, cadence, retention, plays):
                    raise TransferError(f"{path}: required playlist values cannot be null")
                assert size is not None and ratio is not None
                assert cadence is not None and retention is not None and plays is not None
                if (
                    not 1 <= size <= 500
                    or not 0 <= ratio <= 1
                    or not 1 <= cadence <= 365
                    or not 1 <= retention <= 365
                    or not 1 <= plays <= 1000
                ):
                    raise TransferError(f"{path}: playlist values are outside supported limits")
                identity = (username, d["name"])
            else:
                service = _string(d["service"], f"{path}.service")
                _string(d["external_username"], f"{path}.external_username", max_length=255)
                if service not in {"listenbrainz", "navidrome", "discogs"}:
                    raise TransferError(f"{path}.service: unsupported linked account service")
                identity = (username, service)
            if identity in seen:
                raise TransferError(f"{path}: duplicate natural key")
            seen.add(identity)
    root["users"] = users
    return root


def import_settings(
    s: Session, document: Any, *, apply: bool = False, include_secrets: bool = False
) -> dict[str, Any]:
    """Validate, classify and optionally insert rows. Caller owns the transaction."""
    role_names = set(s.scalars(select(Role.name)).all())
    doc = _validate_document(document, include_secrets=include_secrets, known_roles=role_names)
    existing_users = {u.username: u for u in s.scalars(select(User)).all()}
    existing_entities = {(e.kind, e.mbid): e for e in s.scalars(select(WatchedEntity)).all()}
    existing_follows = {
        (u.username, e.kind, e.mbid)
        for f in s.scalars(select(Follow)).all()
        for u in [s.get(User, f.user_id)]
        if u
        for e in [s.get(WatchedEntity, f.entity_id)]
        if e
    }
    existing_playlists = {
        (u.username, p.name)
        for p in s.scalars(select(DiscoveryPlaylist)).all()
        for u in [s.get(User, p.user_id)]
        if u
    }
    existing_links = {
        (u.username, link.service)
        for link in s.scalars(select(UserLink)).all()
        for u in [s.get(User, link.user_id)]
        if u
    }
    rows: dict[str, list[dict[str, Any]]] = {
        k: [] for k in ("users", "entities", "follows", "discovery_playlists", "linked_accounts")
    }
    for user in doc["users"]:
        add = user["username"] not in existing_users
        rows["users"].append({"key": user["username"], "action": "add" if add else "skip"})
    for item in doc["entities"]:
        key = (item["kind"], item["mbid"])
        rows["entities"].append(
            {"key": f"{key[0]}:{key[1]}", "action": "skip" if key in existing_entities else "add"}
        )
    for item in doc["follows"]:
        ident = (item["username"], item["kind"], item["mbid"])
        rows["follows"].append(
            {
                "key": ":".join(ident),
                "action": "skip" if ident in existing_follows else "add",
            }
        )
    for item in doc["discovery_playlists"]:
        ident = (item["username"], item["name"])
        rows["discovery_playlists"].append(
            {
                "key": ":".join(ident),
                "action": "skip" if ident in existing_playlists else "add",
            }
        )
    for item in doc["linked_accounts"]:
        ident = (item["username"], item["service"])
        rows["linked_accounts"].append(
            {
                "key": ":".join(ident),
                "action": "skip" if ident in existing_links else "add",
            }
        )
    app_exists = s.get(AppSettings, 1) is not None
    rows["app_settings"] = (
        [{"key": "app_settings:1", "action": "skip" if app_exists else "add"}]
        if doc["app_settings"]
        else []
    )
    passwordless = [
        user["username"]
        for user in doc["users"]
        if user["username"] not in existing_users and "password_hash" not in user
    ]
    report: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "applied": apply,
        "rows": rows,
        "passwordless_users": passwordless,
    }
    if passwordless:
        report["passwordless_user_notice"] = (
            "These imported accounts are disabled. An admin must set each password and enable "
            "the account on the Users page."
        )
    if not apply:
        return report

    # All document and reference validation is complete before the first mutation.
    user_ids = {name: user.id for name, user in existing_users.items()}
    for user in doc["users"]:
        username = user["username"]
        if username in user_ids:
            continue
        password_hash = user.get("password_hash") or "!password-not-imported!"
        status = user["status"] if user.get("password_hash") else "disabled"
        profile_id = None
        profile = user["matching_profile"]
        if profile:
            # Profiles are mutable through account settings, so never share imported rows.
            row = MatchingProfile(**profile)
            s.add(row)
            s.flush()
            profile_id = row.id
        row_user = User(
            username=username,
            display_name=user["display_name"],
            status=status,
            # older exports predate the approval queue (#296); users then downloaded directly
            download_auto=user.get("download_auto", True),
            password_hash=password_hash,
            matching_profile_id=profile_id,
        )
        s.add(row_user)
        s.flush()
        user_ids[username] = row_user.id
        for role_name in user["roles"]:
            role_id = s.scalar(select(Role.id).where(Role.name == role_name))
            assert role_id is not None
            s.add(UserRole(user_id=row_user.id, role_id=role_id))

    if doc["app_settings"] and not app_exists:
        s.add(AppSettings(id=1, settings_saved_at=datetime.now(UTC), **doc["app_settings"]))
    entity_ids = {(e.kind, e.mbid): e.id for e in s.scalars(select(WatchedEntity)).all()}
    for entity in doc["entities"]:
        key = (entity["kind"], entity["mbid"])
        if key not in entity_ids:
            row = WatchedEntity(kind=key[0], mbid=key[1])
            s.add(row)
            s.flush()
            entity_ids[key] = row.id
    for item, planned in zip(doc["follows"], rows["follows"], strict=True):
        if planned["action"] == "skip":
            continue
        s.add(
            Follow(
                user_id=user_ids[item["username"]],
                entity_id=entity_ids[(item["kind"], item["mbid"])],
                name=item["name"],
                release_types=item["release_types"],
                min_date=item["min_date"],
                weekly_cap=item["weekly_cap"],
            )
        )
    for item, planned in zip(doc["discovery_playlists"], rows["discovery_playlists"], strict=True):
        if planned["action"] == "skip":
            continue
        s.add(
            DiscoveryPlaylist(
                user_id=user_ids[item["username"]],
                **{k: item[k] for k in _PLAYLIST_FIELDS - {"username"}},
            )
        )
    now = datetime.now(UTC)
    for item, planned in zip(doc["linked_accounts"], rows["linked_accounts"], strict=True):
        if planned["action"] == "skip":
            continue
        s.add(
            UserLink(
                user_id=user_ids[item["username"]],
                service=item["service"],
                external_username=item["external_username"],
                secret_encrypted="",
                needs_relink=True,
                verified_at=now,
            )
        )
    return report
