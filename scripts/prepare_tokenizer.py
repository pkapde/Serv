"""Warm tokenizer vocabulary at install/image-build time, before offline runtime."""

import os
from pathlib import Path

import tiktoken

if __name__ == "__main__":
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path("var/tokenizer").resolve()))
    print("Tokenizer ready:", tiktoken.get_encoding("cl100k_base").n_vocab)
