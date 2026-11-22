"""EthOS NAS — Shared Pydantic request/response models."""

from pydantic import BaseModel


class ContainerActionRequest(BaseModel):
    """Body for POST /api/containers/{name}/action."""

    action: str  # start | stop | restart

    model_config = {"extra": "forbid"}


class FilePathRequest(BaseModel):
    """Body for file operations that take a single path."""

    path: str

    model_config = {"extra": "forbid"}


class FileExtractRequest(FilePathRequest):
    """Body for POST /api/files/extract (archive extraction).

    ``dest_dir``  destination folder; defaults to the archive's folder.
    ``subfolder`` None → folder named after the archive (collision-suffixed);
                  "" → straight into ``dest_dir``; any other value → that folder.
    ``password``  encrypted archives only — read from stdin, never logged.
    """

    dest_dir: str | None = None
    subfolder: str | None = None
    overwrite: bool = False
    password: str | None = None


class FileRenameRequest(BaseModel):
    """Body for PUT /api/files/rename."""

    path: str
    new_name: str

    model_config = {"extra": "forbid"}


class MkdirRequest(BaseModel):
    path: str
    model_config = {"extra": "forbid"}


class TrashRestoreRequest(BaseModel):
    name: str
    model_config = {"extra": "forbid"}


class FavoriteRequest(BaseModel):
    """Body for PUT /api/files/favorites."""

    path: str
    starred: bool = True

    model_config = {"extra": "forbid"}


class FileCopyMoveRequest(BaseModel):
    """Body for POST /api/files/copy and POST /api/files/move."""

    sources: list[str]  # absolute source paths to copy or move
    dest_dir: str       # destination directory path

    model_config = {"extra": "forbid"}


class FileDeleteRequest(BaseModel):
    """Body for DELETE /api/files/delete (hard delete)."""

    paths: list[str]  # absolute paths of items to permanently delete

    model_config = {"extra": "forbid"}


class ChunkedInitRequest(BaseModel):
    """Body for POST /api/files/chunked/init."""

    file_id: str | None = None       # auto-generated if omitted
    filename: str                     # name of the uploaded file
    dest_dir: str | None = None      # defaults to /tmp on server

    model_config = {"extra": "forbid"}


class FileContentRequest(BaseModel):
    """Body for PUT /api/files/content (Text Editor save)."""

    path: str
    content: str
    encoding: str = "utf-8"  # utf-8 or latin-1
    mtime: float | None = None  # mtime at load time; 409 if the file changed on disk

    model_config = {"extra": "forbid"}


class VideoCoverRequest(BaseModel):
    """Body for POST /api/files/video-cover (set a custom cover frame)."""

    path: str
    t: float = 0.0  # seconds into the video to grab the frame from
    left_half: bool = False  # crop only the left half (side-by-side / VR goggles videos)

    model_config = {"extra": "forbid"}
