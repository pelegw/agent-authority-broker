"""`python -m aab_plugin_google`: serve the plugin API on :8090.

Same as the image's CMD. One worker: the token cache and the connect
`state` live in this process, and a second worker would not share them.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("aab_plugin_google.main:create_app", factory=True,
                host="0.0.0.0", port=8090, workers=1, access_log=False)
