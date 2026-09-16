# e2e fixtures

## `tiny-model.b64`

A base64 pickle of a fitted `sklearn.dummy.DummyClassifier` — the smallest thing
`POST /api/v1/models` will accept.

**Why a committed fixture and not an inline string.** The API validates the blob at upload:
it unpickles it and checks for `predict`, so a placeholder like `Buffer.from("x")` is refused
with `422 Invalid model: invalid load key`. And it cannot be a pickle of a class defined in the
test, because unpickling happens in the *server* process, which cannot import the test module.

Regenerate with:

```bash
.venv-rulemind/bin/python -c '
import base64, pickle, numpy as np
from sklearn.dummy import DummyClassifier
m = DummyClassifier(strategy="constant", constant=1)
m.fit(np.array([[0.0, 0.0], [1.0, 1.0]]), np.array([0, 1]))
print(base64.b64encode(pickle.dumps(m)).decode())' > e2e/fixtures/tiny-model.b64
```

A pickle is version-sensitive: if a scikit-learn upgrade makes the server refuse to load it,
regenerate rather than pinning the old library.
