"""Raw API JSON as pydantic models. Unknown fields are ignored."""

from pydantic import BaseModel, ConfigDict, Field


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class MbGenre(_Raw):
    name: str
    count: int = 0


class MbArtist(_Raw):
    id: str
    genres: list[MbGenre] = []


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
    track_count: int = Field(default=0, alias="track-count")  # browse omits the tracks


class MbLabel(_Raw):
    name: str


class MbLabelInfo(_Raw):
    catalog_number: str | None = Field(default=None, alias="catalog-number")
    label: MbLabel | None = None


class MbRgRelease(_Raw):
    id: str


class MbReleaseGroup(_Raw):
    id: str | None = None
    title: str = ""
    primary_type: str | None = Field(default=None, alias="primary-type")
    secondary_types: list[str] = Field(default=[], alias="secondary-types")
    artist_credit: list[MbCredit] = Field(default=[], alias="artist-credit")
    releases: list[MbRgRelease] = []  # release-group search hits only
    first_release_date: str | None = Field(default=None, alias="first-release-date")
    genres: list[MbGenre] = []


class MbRelease(_Raw):
    id: str
    title: str
    date: str | None = None
    country: str | None = None
    status: str | None = None
    disambiguation: str = ""
    genres: list[MbGenre] = []
    artist_credit: list[MbCredit] = Field(default=[], alias="artist-credit")
    media: list[MbMedium] = []
    label_info: list[MbLabelInfo] = Field(default=[], alias="label-info")
    release_group: MbReleaseGroup | None = Field(default=None, alias="release-group")
    track_count: int | None = Field(default=None, alias="track-count")  # search hits only


class MbSearch(_Raw):
    releases: list[MbRelease] = []


class MbRgSearch(_Raw):
    release_groups: list[MbReleaseGroup] = Field(default=[], alias="release-groups")
    count: int = Field(default=0, alias="release-group-count")  # browse only


class MbEntity(_Raw):
    id: str
    name: str
    disambiguation: str = ""


class MbEntitySearch(_Raw):
    artists: list[MbEntity] = []
    labels: list[MbEntity] = []


class AcoustIdRecording(_Raw):
    id: str


class AcoustIdHit(_Raw):
    id: str
    score: float
    recordings: list[AcoustIdRecording] = []


class AcoustIdResponse(_Raw):
    status: str
    results: list[AcoustIdHit] = []


class AcoustIdSubmission(_Raw):
    id: int
    status: str = ""


class AcoustIdSubmitResponse(_Raw):
    status: str
    submissions: list[AcoustIdSubmission] = []


class DeezerArtist(_Raw):
    name: str


class DeezerAlbumRaw(_Raw):
    id: int
    title: str
    nb_tracks: int = 0
    release_date: str = ""
    artist: DeezerArtist


class DeezerSearchRaw(_Raw):
    data: list[DeezerAlbumRaw] = []


class DeezerTrackRaw(_Raw):
    title: str
    track_position: int = 0
    disk_number: int = 1
    artist: DeezerArtist


class DeezerTracksRaw(_Raw):
    data: list[DeezerTrackRaw] = []


class DiscogsIdentityRaw(_Raw):
    username: str


class DiscogsHitRaw(_Raw):
    id: int
    title: str  # "Artist - Title"
    year: str = ""


class DiscogsSearchRaw(_Raw):
    results: list[DiscogsHitRaw] = []


class DiscogsArtistRaw(_Raw):
    name: str


class DiscogsTrackRaw(_Raw):
    position: str = ""
    type_: str = "track"
    title: str = ""
    artists: list[DiscogsArtistRaw] = []
    sub_tracks: list["DiscogsTrackRaw"] = []


class DiscogsReleaseRaw(_Raw):
    id: int
    title: str
    year: int = 0
    artists: list[DiscogsArtistRaw] = []
    tracklist: list[DiscogsTrackRaw] = []
