from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
REELS_DIR = STORAGE_DIR / "reels"
AUDIO_DIR = STORAGE_DIR / "audio"
VIDEOS_DIR = STORAGE_DIR / "videos"
SCRIPTS_DIR = STORAGE_DIR / "scripts"
MUSIC_DIR = STORAGE_DIR / "music"


def ensure_storage_dirs() -> None:
    for directory in (REELS_DIR, AUDIO_DIR, VIDEOS_DIR, SCRIPTS_DIR, MUSIC_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def to_storage_relative(path: Path) -> str:
    return path.relative_to(BASE_DIR).as_posix()
