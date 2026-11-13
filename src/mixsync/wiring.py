"""Composition root for the services the worker and the web app share. Only constructs: no
recovery and no startup chains (the worker runs those). A bad key file fails here."""

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.merge import Merger
from mixsync.core.clock import Clock, SystemClock
from mixsync.core.config import Settings
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.names import SqlNameStore
from mixsync.db.secretbox import SecretBox, load_box
from mixsync.library.fileops import FileOps
from mixsync.metadata.coverart import CoverArtClient
from mixsync.metadata.deezer import DeezerClient
from mixsync.metadata.discogs import DiscogsClient
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient

log = logging.getLogger(__name__)
# Test-only (tests/integration): AcoustID and Cover Art Archive calls go to this fake server.
# Deliberately not a Settings field, so it is never on the settings page or in .env.example.
FAKE_SERVICES_ENV = "MIXSYNC_TEST_ONLY_FAKE_SERVICES_URL"


@dataclass(frozen=True)
class Services:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    clock: Clock
    fileops: FileOps
    musicbrainz: MusicBrainzProvider
    merger: Merger
    coverart: CoverArtClient
    deezer: DeezerClient
    discogs: DiscogsClient
    media_server_http: PoliteClient
    listenbrainz_http: PoliteClient
    spotify_http: PoliteClient
    secret_box: SecretBox
    settings_version: datetime | None  # app_settings.updated_at when `settings` was loaded
    fake_services_url: str = ""  # FAKE_SERVICES_ENV; "" is the real services


def build_services(
    settings: Settings,
    session_factory: sessionmaker[Session] | None = None,
    settings_version: datetime | None = None,
) -> Services:
    """`session_factory` lets a caller that already has a database (tests) keep using it."""
    if fake := os.environ.get(FAKE_SERVICES_ENV, "").strip():
        log.warning(
            "%s is set: AcoustID and cover art go to %s. It is for the integration tests "
            "only; unset it in a real deployment.",
            FAKE_SERVICES_ENV,
            fake,
        )
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings))
    engine = session_factory.kw["bind"]
    clock = SystemClock()
    fileops = FileOps(
        Path(settings.data_dir),
        Path(settings.downloads_dir),
        SqlJournal(session_factory, clock),
        clock,
        settings.drop_dir,
        settings.qbittorrent_dir,
        settings.sabnzbd_dir,
    )
    musicbrainz = MusicBrainzProvider(
        PoliteClient("musicbrainz", settings, session_factory),
        settings.mb_base_url,
        SqlNameStore(session_factory, clock),
    )
    merger = Merger(
        session_factory,
        clock,
        musicbrainz,
        fileops,
        settings.path_template,
        tuple(settings.canonical_media),
        tuple(settings.canonical_countries),
        settings.trash_days,
    )
    return Services(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        clock=clock,
        fileops=fileops,
        musicbrainz=musicbrainz,
        merger=merger,
        coverart=CoverArtClient(PoliteClient("coverart", settings, session_factory), fake),
        deezer=DeezerClient(PoliteClient("deezer", settings, session_factory)),
        discogs=DiscogsClient(PoliteClient("discogs", settings, session_factory)),
        media_server_http=PoliteClient("media_server", settings, session_factory),
        listenbrainz_http=PoliteClient("listenbrainz", settings, session_factory),
        spotify_http=PoliteClient("spotify", settings, session_factory),
        secret_box=load_box(settings),
        settings_version=settings_version,
        fake_services_url=fake,
    )
