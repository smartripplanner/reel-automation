from PIL import Image


def ensure_pillow_compat() -> None:
    if not hasattr(Image, "ANTIALIAS") and hasattr(Image, "Resampling"):
        Image.ANTIALIAS = Image.Resampling.LANCZOS
    if not hasattr(Image, "BICUBIC") and hasattr(Image, "Resampling"):
        Image.BICUBIC = Image.Resampling.BICUBIC
    if not hasattr(Image, "BILINEAR") and hasattr(Image, "Resampling"):
        Image.BILINEAR = Image.Resampling.BILINEAR
