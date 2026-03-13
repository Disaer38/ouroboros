"""Dominant Side strategy package for Polymarket paper trading."""
from .config import DominantConfig
from .signal import Signal, SignalGenerator
from .exchange import PaperExchange

__all__ = ["DominantConfig", "Signal", "SignalGenerator", "PaperExchange"]