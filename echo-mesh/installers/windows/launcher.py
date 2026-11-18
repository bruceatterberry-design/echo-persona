"""Entry point for the Windows build (PyInstaller needs a plain script, not a package module)."""
from mesh.winapp import main

if __name__ == "__main__":
    main()
