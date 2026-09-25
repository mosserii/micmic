"""MicMic metering proxy.

A shipped MicMic cannot carry the owner's Jev and Gemini keys, so this small server
holds them, authenticates each device by an opaque bearer token, and meters calls per
token per UTC day. Users who bring their own key never talk to it.

    cd proxy && uv run python -m proxy              # serve on MICMIC_PROXY_PORT (8810)
    cd proxy && uv run python -m proxy.admin mint --plan free
"""
