"""CrowdCode MCP server package."""

import os as _os

# The backend runs on half a CPU. numpy's BLAS and the embedding model's tokenizer otherwise start one thread per
# host core; under a 0.5-CPU quota those threads spend the period's allowance in a burst and the whole process,
# request handlers included, is then throttled until the next period. Set before numpy or tokenizers load.
for _name, _value in (("OMP_NUM_THREADS", "1"), ("OPENBLAS_NUM_THREADS", "1"), ("MKL_NUM_THREADS", "1"),
                      ("RAYON_NUM_THREADS", "1"), ("TOKENIZERS_PARALLELISM", "false")):
    _os.environ.setdefault(_name, _value)
