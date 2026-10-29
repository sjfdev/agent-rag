import os

# Must run before huggingface_hub is imported anywhere in the package.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# Keep metadata lookups short when the mirror is unreachable; the model itself is
# cached locally after the first download.
os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "3")
