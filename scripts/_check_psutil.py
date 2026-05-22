import importlib.util
print("psutil:", importlib.util.find_spec("psutil") is not None)
