import os

# Tests choose their embedder explicitly (tests/test_semantic.py's stand-in, or the real
# model in test_search_eval.py). Without this, the first search in any test would start
# loading the real model in the background and change how later tests rank.
os.environ.setdefault("CROWDCODE_SEMANTIC_SEARCH", "0")
