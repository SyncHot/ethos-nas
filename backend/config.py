"""EthOS NAS — Central configuration via environment variables."""

import os
import secrets
from pathlib import Path

# The value shipped in the repo. Hitting it in production means every token is
# forgeable — main.py checks for it at startup and raises a notification event.
DEFAULT_JWT_SECRET = "change-this-in-production"
APP_VERSION = "2.3.5"

# Checkout root (…/ethos-nas). APK locations resolve from here instead of a
# hardcoded home directory, so a clone works for any user.
INSTALL_DIR = Path(__file__).resolve().parent.parent
# Developer checkouts of the Android apps live next to this repo. Only used as
# a last-resort search path; on a server it resolves to nothing.
DEV_COMPOSE_DIR = Path.home() / "apps" / "compose"

# Ed25519 public key (base64) of the offline release key. It signs both the
# package catalog and the core release index; the matching private key never
# leaves the maintainer's machine. Spec: docs/updates.md.
RELEASE_PUBKEY_B64 = "sXiL7v0droY2BVyttuDt0Bqq6jB7MpknylJo+/iavqE="


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from `<repo>/.env` (0600) without overriding real env.

    Operators can keep secrets out of the unit file: `ETHOS_JWT_SECRET=...`.
    Real environment variables always win, so systemd still has the last word.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _bootstrap_jwt_secret() -> None:
    """Make sure a real JWT secret exists, generating one when unset.

    The systemd unit ships with `ETHOS_JWT_SECRET` commented out, so an install
    that never configured one would otherwise sign tokens with the public
    placeholder. Rather than refusing to start (which turns an upgrade into an
    outage) or silently accepting a forgeable key, a random secret is generated
    once and persisted next to the other runtime state (0600, service user).

    Precedence: real environment value → `.env` file → persisted secret →
    generated secret. `ETHOS_DEBUG=true` and `ETHOS_ALLOW_INSECURE=1` keep the
    old behaviour for development and throwaway boxes.
    """
    if os.getenv("ETHOS_JWT_SECRET"):
        return
    if os.getenv("ETHOS_DEBUG", "false").lower() == "true":
        return
    if os.getenv("ETHOS_ALLOW_INSECURE", "false").lower() in ("1", "true", "yes", "on"):
        return
    data_dir = Path(os.getenv("ETHOS_DATA_DIR", str(Path(__file__).parent / "data")))
    secret_file = data_dir / "jwt_secret"
    try:
        if secret_file.is_file():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if stored:
                os.environ["ETHOS_JWT_SECRET"] = stored
                return
        generated = secrets.token_hex(32)
        data_dir.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(generated + "\n", encoding="utf-8")
        os.chmod(secret_file, 0o600)
        os.environ["ETHOS_JWT_SECRET"] = generated
    except OSError:
        # Unwritable data dir (e.g. a read-only test fixture): leave the value
        # unset — main.py treats the placeholder as a hard startup error unless
        # ETHOS_ALLOW_INSECURE is set.
        return


_bootstrap_jwt_secret()


class Settings:
    HOST: str = os.getenv("ETHOS_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("ETHOS_PORT", "9000"))
    DEBUG: bool = os.getenv("ETHOS_DEBUG", "false").lower() == "true"
    # Explicit opt-in to run with the built-in JWT secret. Without it the app
    # logs at CRITICAL and raises a Notification Center error at startup — but
    # still starts, so an upgrade never takes an existing install down.
    ALLOW_INSECURE: bool = os.getenv("ETHOS_ALLOW_INSECURE", "false").lower() == "true"
    ALLOWED_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv("ETHOS_CORS_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
        if o.strip()
    ]
    STATS_INTERVAL: float = 2.0  # WebSocket tick (seconds)
    JWT_SECRET: str = os.getenv("ETHOS_JWT_SECRET", DEFAULT_JWT_SECRET)
    JWT_EXPIRY_HOURS: int = 24
    # FFmpeg -loglevel for remux/thumbnail jobs. Keep "error" in production;
    # set ETHOS_FFMPEG_LOGLEVEL=info while debugging media pipelines.
    FFMPEG_LOGLEVEL: str = os.getenv("ETHOS_FFMPEG_LOGLEVEL", "error")

    # Directory for JSON state stores (notifications, health monitor, …).
    # Overridable so a second instance (e.g. a test server on another port)
    # can run fully isolated from the production data files.
    DATA_DIR: Path = Path(os.getenv("ETHOS_DATA_DIR", str(Path(__file__).parent / "data")))

    # Offsite backup replication (services/backup_remote.py). Credentials stay in
    # rclone's own config; the API never reads or returns it.
    RCLONE_CONFIG: str = os.getenv("ETHOS_RCLONE_CONFIG", "")

    # ── Packages repositories (services/package_repo.py) ────────────────────
    # Test-only: allow http://127.0.0.1 repos in isolated tests. Never enable
    # this on a real NAS.
    PACKAGES_ALLOW_INSECURE: bool = os.getenv("ETHOS_PACKAGES_ALLOW_INSECURE", "false").lower() in ("1", "true", "yes", "on")
    # Allow catalog fetches from private/loopback addresses (LAN repos).
    PACKAGES_ALLOW_PRIVATE: bool = os.getenv("ETHOS_PACKAGES_ALLOW_PRIVATE", "false").lower() in ("1", "true", "yes", "on")
    PACKAGES_MAX_INDEX_BYTES: int = int(os.getenv("ETHOS_PACKAGES_MAX_INDEX_BYTES", str(5 * 1024 * 1024)))
    PACKAGES_MAX_MANIFEST_BYTES: int = int(os.getenv("ETHOS_PACKAGES_MAX_MANIFEST_BYTES", str(2 * 1024 * 1024)))
    PACKAGES_MAX_ARTIFACT_BYTES: int = int(os.getenv("ETHOS_PACKAGES_MAX_ARTIFACT_BYTES", str(256 * 1024 * 1024)))
    PACKAGES_TTL_SECONDS: int = int(os.getenv("ETHOS_PACKAGES_TTL_SECONDS", "900"))
    # Seed the official signed source on a fresh install. Tests set this to 0
    # so they never depend on GitHub availability.
    PACKAGES_DEFAULT_SOURCE: bool = os.getenv("ETHOS_PACKAGES_DEFAULT_SOURCE", "true").lower() in ("1", "true", "yes", "on")
    PACKAGES_DEFAULT_SOURCE_NAME: str = os.getenv("ETHOS_PACKAGES_DEFAULT_SOURCE_NAME", "EthOS Community Packages")
    PACKAGES_DEFAULT_SOURCE_URL: str = os.getenv(
        "ETHOS_PACKAGES_DEFAULT_SOURCE_URL",
        "https://raw.githubusercontent.com/SyncHot/ethos-packages/main/index.json",
    )
    PACKAGES_DEFAULT_SOURCE_KEY: str = os.getenv(
        "ETHOS_PACKAGES_DEFAULT_SOURCE_KEY",
        RELEASE_PUBKEY_B64,
    )

    # ── Core update check (services/update_service.py, docs/updates.md) ─────
    # Reads a signed release index published from the public repository. This
    # is the *check* path only: nothing is installed from here.
    UPDATE_ENABLED: bool = os.getenv("ETHOS_UPDATE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    UPDATE_INDEX_URL: str = os.getenv(
        "ETHOS_UPDATE_INDEX_URL",
        "https://raw.githubusercontent.com/SyncHot/ethos-nas/master/releases/index.json",
    )
    UPDATE_PUBKEY: str = os.getenv("ETHOS_UPDATE_PUBKEY", RELEASE_PUBKEY_B64)
    # Root helper installed by setup_scripts/setup_service.sh (argument-free sudo).
    UPDATE_HELPER: str = os.getenv("ETHOS_UPDATE_HELPER", "/usr/local/bin/ethos-update")
    UPDATE_MAX_INDEX_BYTES: int = int(os.getenv("ETHOS_UPDATE_MAX_INDEX_BYTES", str(256 * 1024)))
    # A cached result younger than this is returned without a network round-trip.
    UPDATE_TTL_SECONDS: int = int(os.getenv("ETHOS_UPDATE_TTL_SECONDS", "3600"))
    # Background check cadence (the first run happens one interval after start).
    UPDATE_CHECK_HOURS: float = float(os.getenv("ETHOS_UPDATE_CHECK_HOURS", "24"))

    # ── System Health Monitor (services/health_monitor_service.py) ───────────
    # How often the background checker runs (seconds).
    HEALTH_CHECK_INTERVAL: int = int(os.getenv("ETHOS_HEALTH_CHECK_INTERVAL", "300"))
    # Warn when any real mount's usage reaches this percent.
    DISK_WARN_PERCENT: float = float(os.getenv("ETHOS_DISK_WARN_PERCENT", "90"))
    # Clear the alert (and emit a recovery event) once usage drops below this
    # percent. Kept below DISK_WARN_PERCENT for hysteresis so a disk hovering
    # at the threshold doesn't flap between warning and recovered.
    DISK_RECOVER_PERCENT: float = float(os.getenv("ETHOS_DISK_RECOVER_PERCENT", "85"))
    # While a condition persists, re-alert at most once per this many seconds
    # (default 6 h) so a full disk doesn't spam the Notification Center.
    HEALTH_ALERT_COOLDOWN: int = int(os.getenv("ETHOS_HEALTH_ALERT_COOLDOWN", "21600"))
    # RAM usage thresholds for the memory-pressure check (same semantics as
    # the disk pair; overridable at runtime via PUT /api/health/config).
    MEMORY_WARN_PERCENT: float = float(os.getenv("ETHOS_MEMORY_WARN_PERCENT", "90"))
    MEMORY_RECOVER_PERCENT: float = float(os.getenv("ETHOS_MEMORY_RECOVER_PERCENT", "85"))

    # Temperature warning threshold (°C) for any sensor; recovery is fixed at
    # warn − 5°C (health_monitor_service.TEMP_HYSTERESIS_C).
    TEMP_WARN_CELSIUS: float = float(os.getenv("ETHOS_TEMP_WARN_CELSIUS", "85"))

    # ── Network throughput history (services/net_stats_service.py) ───────────
    # How often the sampler records total RX/TX counters (seconds).
    NET_STATS_INTERVAL: int = int(os.getenv("ETHOS_NET_STATS_INTERVAL", "60"))

    # ── Android APK delivery (routers/updates.py) ────────────────────────────
    # Per-app search order; the newest (version_code, mtime) wins.
    #   • `apks/<app>/` inside the install dir is the deployed location — every
    #     app's build.sh copies the APK + sidecar manifest there.
    #   • The `~/apps/compose/ethos-<app>/` entries are a developer-checkout
    #     convenience only (Gradle output dir); they don't exist on a server.
    APK_DEFAULTS: dict[str, list[str]] = {
        "moments": [
            str(INSTALL_DIR / "apks" / "moments" / "app-release.apk"),
            str(INSTALL_DIR / "apks" / "moments" / "app-debug.apk"),
            str(DEV_COMPOSE_DIR / "ethos-moments/app/build/outputs/apk/release/app-release.apk"),
            str(DEV_COMPOSE_DIR / "ethos-moments/app/build/outputs/apk/debug/app-debug.apk"),
        ],
        "music": [
            str(INSTALL_DIR / "apks" / "music" / "app-release.apk"),
            str(INSTALL_DIR / "apks" / "music" / "app-debug.apk"),
            str(DEV_COMPOSE_DIR / "ethos-music/app/build/outputs/apk/release/app-release.apk"),
            str(DEV_COMPOSE_DIR / "ethos-music/app/build/outputs/apk/debug/app-debug.apk"),
        ],
        "video": [
            str(INSTALL_DIR / "apks" / "video" / "app-release.apk"),
            str(INSTALL_DIR / "apks" / "video" / "app-debug.apk"),
            str(DEV_COMPOSE_DIR / "ethos-video/app/build/outputs/apk/release/app-release.apk"),
            str(DEV_COMPOSE_DIR / "ethos-video/app/build/outputs/apk/debug/app-debug.apk"),
        ],
    }
    # Explicit override per app (ETHOS_MOMENTS_APK_PATH …).
    APK_PATHS: dict[str, str] = {
        app: os.getenv(f"ETHOS_{app.upper()}_APK_PATH", "")
        for app in ("moments", "music", "video")
    }
    # systemd unit that runs EthOS itself (self-management router).
    SERVICE_UNIT: str = os.getenv("ETHOS_SERVICE_UNIT", "ethos-nas.service")

    # ── Retention (services/retention_service.py) ────────────────────────────
    # How often the sweep runs (seconds, minimum 300).
    RETENTION_INTERVAL: int = int(os.getenv("ETHOS_RETENTION_INTERVAL", "21600"))
    # Abandoned chunked uploads and playback transcodes older than this go away.
    TEMP_CHUNK_TTL_HOURS: int = int(os.getenv("ETHOS_TEMP_CHUNK_TTL_HOURS", "24"))
    # File Manager recycle bin retention.
    TRASH_RETENTION_DAYS: int = int(os.getenv("ETHOS_TRASH_RETENTION_DAYS", "30"))
    # LRU caps for the two thumbnail caches.
    THUMB_CACHE_MAX_FILES: int = int(os.getenv("ETHOS_THUMB_CACHE_MAX_FILES", "20000"))
    VIDEO_THUMB_CACHE_MAX_FILES: int = int(os.getenv("ETHOS_VIDEO_THUMB_CACHE_MAX_FILES", "5000"))

    # ── Unified log store (services/log_service.py, docs/logs.md) ────────────
    # In-memory ring size (entries), per-file size cap and generation count.
    # 5000 × 1 MiB × 5 keeps the viewer useful while capping the disk at ~5 MiB.
    LOG_RING_SIZE: int = int(os.getenv("ETHOS_LOG_RING_SIZE", "5000"))
    LOG_MAX_BYTES: int = int(os.getenv("ETHOS_LOG_MAX_BYTES", str(1024 * 1024)))
    LOG_FILES: int = int(os.getenv("ETHOS_LOG_FILES", "5"))
    # Identical events inside this window are folded into one row with `count`.
    LOG_DEDUPE_WINDOW: float = float(os.getenv("ETHOS_LOG_DEDUPE_WINDOW", "30"))
    # Browser/package ingest budget per user (events per minute).
    LOG_INGEST_PER_MIN: int = int(os.getenv("ETHOS_LOG_INGEST_PER_MIN", "120"))
    # Base level for the Python loggers (debug | info | warning | error).
    LOG_LEVEL: str = os.getenv("ETHOS_LOG_LEVEL", "info").lower()

    # ── State snapshots (services/config_archive.py) ─────────────────────────
    # Daily archive of every JSON store + gallery index in <DATA_DIR>/snapshots/.
    CONFIG_SNAPSHOT_ENABLED: bool = os.getenv("ETHOS_CONFIG_SNAPSHOT", "1") not in ("0", "false", "False")
    CONFIG_SNAPSHOT_INTERVAL: int = int(os.getenv("ETHOS_CONFIG_SNAPSHOT_INTERVAL", "86400"))
    CONFIG_SNAPSHOT_KEEP: int = int(os.getenv("ETHOS_CONFIG_SNAPSHOT_KEEP", "7"))


settings = Settings()
