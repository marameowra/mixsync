"""Composition root for the services the worker and the web app share. Only constructs: no
recovery and no startup chains (the worker runs those). A bad key file fails here."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from mixsync.acquire.merge import Merger
from mixsync.core.clock import Clock, SystemClock
from mixsync.core.config import Settings
from mixsync.db.engine import make_engine, make_session_factory
from mixsync.db.journal import SqlJournal
from mixsync.db.secretbox import SecretBox, load_box
from mixsync.library.fileops import FileOps
from mixsync.metadata.coverart import CoverArtClient
from mixsync.metadata.deezer import DeezerClient
from mixsync.metadata.musicbrainz import MusicBrainzProvider
from mixsync.ratelimit.client import PoliteClient


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
    navidrome_http: PoliteClient
    listenbrainz_http: PoliteClient
    secret_box: SecretBox


def build_services(
    settings: Settings, session_factory: sessionmaker[Session] | None = None
) -> Services:
    """`session_factory` lets a caller that already has a database (tests) keep using it."""
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
    )
    musicbrainz = MusicBrainzProvider(
        PoliteClient("musicbrainz", settings, session_factory), settings.mb_base_url
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
        coverart=CoverArtClient(PoliteClient("coverart", settings, session_factory)),
        deezer=DeezerClient(PoliteClient("deezer", settings, session_factory)),
        navidrome_http=PoliteClient("navidrome", settings, session_factory),
        listenbrainz_http=PoliteClient("listenbrainz", settings, session_factory),
        secret_box=load_box(settings),
    )
