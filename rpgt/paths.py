"""Where things live. ROOT is the folder holding run-ui.bat; user files stay there."""
import os

PACKAGE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PACKAGE)

CONFIG_PATH = os.path.join(ROOT, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(ROOT, "config.example.json")
GLOSSARY_PATH = os.path.join(ROOT, "glossary.json")

DATA_DIR = os.path.join(ROOT, "data")            # runtime files: caches and logs
UNITY_DIR = os.path.join(ROOT, "unity-kits")     # BepInEx downloads and prebuilt XUnity kits
DOWNLOADS_DIR = os.path.join(UNITY_DIR, "downloads")
