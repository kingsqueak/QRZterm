"""Keep textual-image from probing the real terminal during tests."""

try:
    import textual_image._terminal as _terminal
except ImportError:
    pass
else:
    if not hasattr(_terminal.probe_terminal, "_result"):
        _terminal.probe_terminal._result = _terminal.TerminalCapabilities(
            _terminal.CellSize(10, 20),
            sixel=False,
            tgp=False,
        )
