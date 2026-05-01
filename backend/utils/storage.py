from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
PROJECT_ROOT = BASE_DIR.parent                             # project root (reel-automation-dashboard/)
STORAGE_DIR = BASE_DIR / "storage"
OUTPUT_DIR = PROJECT_ROOT / "output"                       # final reels visible to user
REELS_DIR = OUTPUT_DIR                                     # alias — renders save here
AUDIO_DIR = STORAGE_DIR / "audio"
VIDEOS_DIR = STORAGE_DIR / "videos"
SCRIPTS_DIR = STORAGE_DIR / "scripts"
MUSIC_DIR = STORAGE_DIR / "music"


def ensure_storage_dirs() -> None:
    for directory in (OUTPUT_DIR, AUDIO_DIR, VIDEOS_DIR, SCRIPTS_DIR, MUSIC_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def to_storage_relative(path: Path) -> str:
    """Return a posix path relative to BASE_DIR, or an absolute posix path if outside."""
    try:
        return path.relative_to(BASE_DIR).as_posix()
    except ValueError:
        # Path is outside BASE_DIR (e.g. OUTPUT_DIR in project root) — return absolute
        return path.as_posix()
