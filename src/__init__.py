import os

# Must run before huggingface_hub is imported anywhere in the package.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
