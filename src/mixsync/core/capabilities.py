from enum import StrEnum


class Capability(StrEnum):
    request = "request"
    download_auto = "download.auto"
    approve = "approve"
    library_edit = "library.edit"
    workflow_edit = "workflow.edit"
    admin = "admin"


DEFAULT_ROLES: dict[str, frozenset[Capability]] = {
    "admin": frozenset(Capability),
    "user": frozenset({Capability.request, Capability.download_auto}),
}
