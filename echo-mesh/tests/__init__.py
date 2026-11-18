
import os
os.environ.setdefault("ECHOMESH_SPLIT_ID", "1")     # pretend computers on one real one must not look like duplicates

# On Windows the coordinator tests need the openssl program (the coordinator makes its certificate with it; the Windows app itself never does).
# Git for Windows ships one; use it for the test run if openssl is not already on the PATH. Nothing is changed outside this process.
import os as _os, shutil as _sh
if _os.name == "nt" and not _sh.which("openssl"):
    for _d in (r"C:\Program Files\Git\mingw64\bin", r"C:\Program Files\Git\usr\bin"):
        if _os.path.exists(_os.path.join(_d, "openssl.exe")):
            _os.environ["PATH"] = _d + _os.pathsep + _os.environ["PATH"]
            break
