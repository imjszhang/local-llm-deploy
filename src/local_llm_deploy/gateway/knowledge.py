"""Knowledge collector path mapping. No model credentials or inference policy."""
from __future__ import annotations

from .apps import slash_redirect


def knowledge_redirect_location(path, prefix='/knowledge'):
    return slash_redirect(path, prefix)
