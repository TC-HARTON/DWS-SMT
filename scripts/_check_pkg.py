import importlib.util
for m in ("scipy", "numba"):
    print(f"{m}: {importlib.util.find_spec(m) is not None}")
