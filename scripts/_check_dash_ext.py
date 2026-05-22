import importlib

for sub in ["streaming", "events", "snippets", "utils", "enrich"]:
    try:
        m = importlib.import_module(f"dash_extensions.{sub}")
        names = [n for n in dir(m) if not n.startswith("_")]
        print(f"=== dash_extensions.{sub} ===")
        print(names)
        print()
    except Exception as exc:
        print(f"  {sub}: import failed: {exc}")
