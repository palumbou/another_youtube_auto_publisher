"""Pluggable transcription and content-analysis providers."""


class ProviderError(RuntimeError):
    """A provider failed in a way that must not be retried blindly."""
