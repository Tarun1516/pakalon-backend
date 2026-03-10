"""Python startup tweaks for local development.

This module is imported automatically by Python during startup when it is
present on the import path. We use it to force the Windows selector event loop
policy early enough for psycopg's async driver, including when the app is
started via the `uvicorn` CLI.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
