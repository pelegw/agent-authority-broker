"""`python -m aab_plugin_whatsapp`: serve the plugin API on :8090.

Same as the image's CMD. One worker: the service keeps no shared state that a
second worker could coordinate, and there is no reason to have two.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("aab_plugin_whatsapp.main:create_app", factory=True,
                host="0.0.0.0", port=8090, workers=1, access_log=False)
