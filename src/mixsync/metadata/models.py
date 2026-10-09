"""Raw API JSON as pydantic models. Unknown fields are ignored."""

from pydantic import BaseModel, ConfigDict, Field


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class MbArtist(_Raw):
    id: str


class MbCredit(_Raw):
    name: str
    joinphrase: str = ""
    artist: MbArtist


class MbRecording(_Raw):
    id: str
    title: str
    length: int | None = None
    artist_credit: list[MbCredit] = Field(default=[], alias="artist-credit")


class MbTrack(_Raw):
    title: str
    position: int
    length: int | None = None
    recording: MbRecording
    artist_credit: list[MbCredit] = Field(default=[], alias="artist-credit")


class MbMedium(_Raw):
    position: int = 1  # search hits omit it
    format: str | None = None
    tracks: list[MbTrack] = []


class MbLabel(_Raw):
    name: str


class MbLabelInfo(_Raw):
    catalog_number: str | None = Field(default=None, alias="catalog-number")
    label: MbLabel | None = None


class MbReleaseGroup(_Raw):
    id: str | None = None
    first_release_date: str | None = Field(default=None, alias="first-release-date")


class MbRelease(_Raw):
    id: str
    title: str
    date: str | None = None
    country: str | None = None
    disambiguation: str = ""
    artist_credit: list[MbCredit] = Field(default=[], alias="artist-credit")
    media: list[MbMedium] = []
    label_info: list[MbLabelInfo] = Field(default=[], alias="label-info")
    release_group: MbReleaseGroup | None = Field(default=None, alias="release-group")
    track_count: int | None = Field(default=None, alias="track-count")  # search hits only


class MbSearch(_Raw):
    releases: list[MbRelease] = []


class AcoustIdRecording(_Raw):
    id: str


class AcoustIdHit(_Raw):
    id: str
    score: float
    recordings: list[AcoustIdRecording] = []


class AcoustIdResponse(_Raw):
    status: str
    results: list[AcoustIdHit] = []
