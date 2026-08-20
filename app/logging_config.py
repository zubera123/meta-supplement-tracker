"""Central logging setup."""

import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure concise process-wide logging suitable for Railway logs."""

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
