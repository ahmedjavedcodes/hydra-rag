from typing import List

from PIL import Image

from src.loaders.base import LoadContext, LoaderError, image_element, register
from src.schema import Element


@register(".png", ".jpg", ".jpeg", ".webp")
def load_image(ctx: LoadContext) -> List[Element]:
    """One image element: the vision model's description, including any text visible in the image."""
    try:
        with Image.open(ctx.path) as img:
            img.verify()
    except Exception:
        raise LoaderError("file is corrupt or not an image")
    with open(ctx.path, "rb") as f:
        raw = f.read()
    ctx.take_image_slot()
    (description, error), = ctx.describe_images([(raw, "", "image")])
    return [image_element(ctx, description, error, location={}, label="image", title=ctx.filename)]
