"""`python -m aab_plugin_github`: serve the plugin API on :8090.

Same as the image's CMD. One worker: the token cache is per process, and a
single process keeps the secret store's writes serialized.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("aab_plugin_github.main:create_app", factory=True,
                host="0.0.0.0", port=8090, workers=1, access_log=False)
