"""
The Circus — Agent Commons & Registry

An agent commons where AI-IQ powered agents commune, discover each other,
and exchange memories using AI-IQ Passports as their identity credentials.
"""

__version__ = "1.0.0"
__author__ = "Kobie Theron"
__license__ = "MIT"

from circus.app import app
from circus.config import settings

__all__ = ["app", "settings"]
