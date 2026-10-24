"""File loaders. Importing this package registers every loader in the extension registry.

To support a new format, add a module with a function decorated with `@register(".ext")`
that turns a LoadContext into a list of Elements, and import it below.
"""

from src.loaders.base import (  # noqa: F401
    UNSUPPORTED_HINTS,
    LoadContext,
    LoaderError,
    get_loader,
    register,
    supported_extensions,
)
from src.loaders import (  # noqa: F401
    docx_loader,
    image_loader,
    pdf_loader,
    pptx_loader,
    table_loader,
    text_loader,
    web_loader,
)
