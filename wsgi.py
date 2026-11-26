"""WSGI entry point for hosting (PythonAnywhere or any WSGI server).

The host imports `application` from this file. Optional environment variables:
    PARTYBANK_BASE_URL   public address used in card URLs, e.g. https://name.pythonanywhere.com
    PARTYBANK_TZ         time zone for displayed times, e.g. Australia/Sydney (servers run in UTC)
"""

import os
import time

if os.environ.get("PARTYBANK_TZ"):
    os.environ["TZ"] = os.environ["PARTYBANK_TZ"]
    time.tzset()

from partybank import create_app  # noqa: E402

application = create_app()
