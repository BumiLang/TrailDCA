try:
    # On Windows, corporate-managed machines often intercept TLS via a
    # locally-installed root CA (already trusted by the OS/browser). Python's
    # bundled certifi store doesn't know about it, so plain `requests`/`urllib3`
    # calls (used by both the Toss client and google-auth/gspread) fail with
    # CERTIFICATE_VERIFY_FAILED. `truststore` makes the stdlib ssl module use
    # the OS trust store instead -- this *validates* against the real trust
    # anchors rather than disabling verification. Injected here, at package
    # import time, so it applies no matter which submodule is imported first.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass
